from collections import Counter
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from agent_eval.api.dependencies import DatabaseSession, WorkspaceCsrf, WorkspaceSession
from agent_eval.api.mutation_guard import sqlite_mutation_guard
from agent_eval.api.routes.workspace import (
    attempt_response,
    available_version,
    ensure_prompt_fits,
    request_fingerprint,
    resolve_idempotency_key,
    workspace_error,
    workspace_user_id,
)
from agent_eval.api.schemas.workspace import (
    WorkspaceComparisonCounts,
    WorkspaceComparisonCreate,
    WorkspaceComparisonItem,
    WorkspaceComparisonList,
    WorkspaceComparisonPatch,
    WorkspaceComparisonResponse,
    WorkspaceConversationAgent,
)
from agent_eval.db.base import ensure_utc, utc_now
from agent_eval.db.enums import WorkspaceAttemptStatus, WorkspaceMessageRole
from agent_eval.db.models import (
    AgentVersion,
    WorkspaceAttempt,
    WorkspaceComparison,
    WorkspaceConversation,
    WorkspaceMessage,
)
from agent_eval.workspace.prompt import format_workspace_comparison_prompt

router = APIRouter(prefix="/api/v1/workspace", tags=["agent-workspace-comparisons"])
ACTIVE_STATUSES = (WorkspaceAttemptStatus.QUEUED, WorkspaceAttemptStatus.RUNNING)


async def owned_comparison(
    database: AsyncSession, comparison_id: UUID, owner_user_id: UUID, *, lock: bool = False
) -> WorkspaceComparison:
    statement = select(WorkspaceComparison).where(
        WorkspaceComparison.id == comparison_id,
        WorkspaceComparison.owner_user_id == owner_user_id,
    )
    if lock:
        statement = statement.with_for_update()
    comparison = await database.scalar(statement)
    if comparison is None:
        raise workspace_error(404, "comparison_not_found", "Comparison not found")
    return comparison


async def comparison_conversations(
    database: AsyncSession, comparison_id: UUID
) -> list[WorkspaceConversation]:
    return list(
        await database.scalars(
            select(WorkspaceConversation)
            .options(
                joinedload(WorkspaceConversation.pinned_agent_version).joinedload(
                    AgentVersion.agent
                ),
                selectinload(WorkspaceConversation.attempts).joinedload(
                    WorkspaceAttempt.assistant_message
                ),
                selectinload(WorkspaceConversation.attempts).joinedload(
                    WorkspaceAttempt.annotation
                ),
                selectinload(WorkspaceConversation.attempts).joinedload(
                    WorkspaceAttempt.evaluation_case
                ),
            )
            .where(WorkspaceConversation.comparison_id == comparison_id)
            .order_by(WorkspaceConversation.comparison_position)
        )
    )


def comparison_state(
    attempts: list[WorkspaceAttempt], cancel_requested: bool
) -> Literal["queued", "running", "canceling", "completed"]:
    statuses = {attempt.status for attempt in attempts}
    if not statuses - {WorkspaceAttemptStatus.QUEUED}:
        return "queued"
    if statuses & set(ACTIVE_STATUSES):
        return "canceling" if cancel_requested else "running"
    return "completed"


async def comparison_response(
    database: AsyncSession, comparison: WorkspaceComparison
) -> WorkspaceComparisonResponse:
    conversations = await comparison_conversations(database, comparison.id)
    items: list[WorkspaceComparisonItem] = []
    attempts: list[WorkspaceAttempt] = []
    for conversation in conversations:
        if len(conversation.attempts) != 1 or conversation.comparison_position is None:
            raise RuntimeError("comparison lane has an invalid attempt relationship")
        attempt = conversation.attempts[0]
        attempts.append(attempt)
        version = conversation.pinned_agent_version
        items.append(
            WorkspaceComparisonItem(
                position=conversation.comparison_position,
                conversation_id=conversation.id,
                agent=WorkspaceConversationAgent(
                    agent_id=version.agent.id,
                    agent_version_id=version.id,
                    name=version.agent.name,
                    provider=version.agent.provider,
                    version=version.version,
                    visibility=(
                        "private" if version.agent.owner_user_id is not None else "public"
                    ),
                    model=version.private_model,
                ),
                attempt=attempt_response(attempt),
                evaluation_case_id=(
                    attempt.evaluation_case.id if attempt.evaluation_case is not None else None
                ),
            )
        )
    counts = Counter(attempt.status for attempt in attempts)
    updated_at = max(
        ensure_utc(value)
        for value in [comparison.updated_at, *(attempt.updated_at for attempt in attempts)]
    )
    return WorkspaceComparisonResponse(
        id=comparison.id,
        title=comparison.title,
        prompt=comparison.prompt,
        status=comparison_state(attempts, comparison.cancel_requested_at is not None),
        counts=WorkspaceComparisonCounts(
            queued=counts[WorkspaceAttemptStatus.QUEUED],
            running=counts[WorkspaceAttemptStatus.RUNNING],
            succeeded=counts[WorkspaceAttemptStatus.SUCCEEDED],
            failed=counts[WorkspaceAttemptStatus.FAILED],
            canceled=counts[WorkspaceAttemptStatus.CANCELED],
        ),
        revision=comparison.revision,
        archived_at=comparison.archived_at,
        cancel_requested_at=comparison.cancel_requested_at,
        created_at=comparison.created_at,
        updated_at=updated_at,
        items=items,
    )


async def existing_comparison(
    database: AsyncSession,
    owner_user_id: UUID,
    key: str,
    fingerprint: str,
) -> WorkspaceComparison | None:
    comparison = await database.scalar(
        select(WorkspaceComparison).where(
            WorkspaceComparison.owner_user_id == owner_user_id,
            WorkspaceComparison.idempotency_key == key,
        )
    )
    if comparison is not None and comparison.request_fingerprint != fingerprint:
        raise workspace_error(409, "idempotency_conflict", "Idempotency key was already used")
    return comparison


@router.post(
    "/comparisons",
    response_model=WorkspaceComparisonResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_comparison(
    payload: WorkspaceComparisonCreate,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
    idempotency_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WorkspaceComparisonResponse:
    owner_user_id = workspace_user_id(auth_session)
    key = resolve_idempotency_key(payload.idempotency_key, idempotency_header)
    guard_key = f"workspace-comparison-create:{owner_user_id}:{key}"
    async with sqlite_mutation_guard(request, database, guard_key):
        return await create_comparison_locked(payload, database, owner_user_id, key)


async def create_comparison_locked(
    payload: WorkspaceComparisonCreate,
    database: AsyncSession,
    owner_user_id: UUID,
    key: str,
) -> WorkspaceComparisonResponse:
    fingerprint = request_fingerprint(
        {
            "agent_version_ids": [str(version_id) for version_id in payload.agent_version_ids],
            "operation": "compare",
            "prompt": payload.prompt,
            "title": payload.title,
        }
    )
    existing = await existing_comparison(database, owner_user_id, key, fingerprint)
    if existing is not None:
        return await comparison_response(database, existing)

    versions: list[AgentVersion] = []
    for version_id in payload.agent_version_ids:
        version = await available_version(database, version_id, owner_user_id)
        if version is None:
            raise workspace_error(
                409, "agent_version_unavailable", "An Agent version is unavailable"
            )
        versions.append(version)
    if len({version.agent_id for version in versions}) != len(versions):
        raise workspace_error(
            422, "agent_identity_duplicate", "Choose only one version of each Agent"
        )

    formatted = format_workspace_comparison_prompt(payload.prompt)
    for version in versions:
        ensure_prompt_fits(formatted.characters, version)

    comparison = WorkspaceComparison(
        id=uuid4(),
        owner_user_id=owner_user_id,
        title=payload.title or payload.prompt.splitlines()[0][:200],
        prompt=payload.prompt,
        idempotency_key=key,
        request_fingerprint=fingerprint,
    )
    database.add(comparison)
    try:
        await database.flush()
    except IntegrityError as error:
        await database.rollback()
        duplicate = await existing_comparison(database, owner_user_id, key, fingerprint)
        if duplicate is not None:
            return await comparison_response(database, duplicate)
        raise workspace_error(
            409, "comparison_conflict", "Comparison changed concurrently"
        ) from error
    for position, version in enumerate(versions, start=1):
        conversation = WorkspaceConversation(
            id=uuid4(),
            owner_user_id=owner_user_id,
            pinned_agent_version_id=version.id,
            title=comparison.title,
            last_turn_no=1,
            revision=1,
            comparison_id=comparison.id,
            comparison_position=position,
        )
        database.add(conversation)
        await database.flush()
        message = WorkspaceMessage(
            id=uuid4(),
            conversation_id=conversation.id,
            turn_no=1,
            role=WorkspaceMessageRole.USER,
            variant_no=1,
            active=True,
            content=payload.prompt,
        )
        database.add(message)
        await database.flush()
        database.add(
            WorkspaceAttempt(
                conversation_id=conversation.id,
                user_message_id=message.id,
                agent_version_id=version.id,
                generation_no=1,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                status=WorkspaceAttemptStatus.QUEUED,
                prompt_hash=formatted.sha256,
                prompt_characters=formatted.characters,
                prompt_format=formatted.format_version,
                annotation=None,
            )
        )
    try:
        await database.commit()
    except IntegrityError as error:
        await database.rollback()
        duplicate = await existing_comparison(database, owner_user_id, key, fingerprint)
        if duplicate is not None:
            return await comparison_response(database, duplicate)
        raise workspace_error(
            409, "comparison_conflict", "Comparison changed concurrently"
        ) from error
    return await comparison_response(database, comparison)


@router.get("/comparisons", response_model=WorkspaceComparisonList)
async def list_comparisons(
    response: Response,
    database: DatabaseSession,
    auth_session: WorkspaceSession,
    limit: Annotated[int, Query(ge=1, le=50)] = 30,
    include_archived: bool = True,
) -> WorkspaceComparisonList:
    statement = select(WorkspaceComparison).where(
        WorkspaceComparison.owner_user_id == workspace_user_id(auth_session)
    )
    if not include_archived:
        statement = statement.where(WorkspaceComparison.archived_at.is_(None))
    comparisons = list(
        await database.scalars(
            statement.order_by(
                WorkspaceComparison.created_at.desc(), WorkspaceComparison.id.desc()
            )
            .limit(limit)
        )
    )
    response.headers["Cache-Control"] = "private, no-store"
    return WorkspaceComparisonList(
        items=[await comparison_response(database, item) for item in comparisons]
    )


@router.get("/comparisons/{comparison_id}", response_model=WorkspaceComparisonResponse)
async def get_comparison(
    comparison_id: UUID,
    response: Response,
    database: DatabaseSession,
    auth_session: WorkspaceSession,
) -> WorkspaceComparisonResponse:
    comparison = await owned_comparison(
        database, comparison_id, workspace_user_id(auth_session)
    )
    response.headers["Cache-Control"] = "private, no-store"
    return await comparison_response(database, comparison)


@router.patch(
    "/comparisons/{comparison_id}", response_model=WorkspaceComparisonResponse
)
async def patch_comparison(
    comparison_id: UUID,
    payload: WorkspaceComparisonPatch,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> WorkspaceComparisonResponse:
    owner_user_id = workspace_user_id(auth_session)
    async with sqlite_mutation_guard(
        request, database, f"workspace-comparison:{comparison_id}"
    ):
        comparison = await owned_comparison(
            database, comparison_id, owner_user_id, lock=True
        )
        if comparison.revision != payload.expected_revision:
            raise workspace_error(409, "revision_conflict", "Comparison revision changed")
        if payload.title is not None:
            comparison.title = payload.title
        if payload.archived is not None:
            comparison.archived_at = utc_now() if payload.archived else None
        comparison.revision += 1
        comparison.updated_at = utc_now()
        await database.commit()
        return await comparison_response(database, comparison)


@router.post(
    "/comparisons/{comparison_id}/cancel", response_model=WorkspaceComparisonResponse
)
async def cancel_comparison(
    comparison_id: UUID,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> WorkspaceComparisonResponse:
    owner_user_id = workspace_user_id(auth_session)
    async with sqlite_mutation_guard(
        request, database, f"workspace-comparison:{comparison_id}"
    ):
        return await cancel_comparison_locked(comparison_id, database, owner_user_id)


async def cancel_comparison_locked(
    comparison_id: UUID,
    database: AsyncSession,
    owner_user_id: UUID,
) -> WorkspaceComparisonResponse:
    comparison = await owned_comparison(database, comparison_id, owner_user_id, lock=True)
    attempts = list(
        await database.scalars(
            select(WorkspaceAttempt)
            .join(
                WorkspaceConversation,
                WorkspaceConversation.id == WorkspaceAttempt.conversation_id,
            )
            .where(WorkspaceConversation.comparison_id == comparison.id)
            .order_by(WorkspaceAttempt.id)
            .with_for_update(of=WorkspaceAttempt)
        )
    )
    now = utc_now()
    active = False
    for attempt in attempts:
        if attempt.status == WorkspaceAttemptStatus.QUEUED:
            active = True
            attempt.status = WorkspaceAttemptStatus.CANCELED
            attempt.cancel_requested_at = now
            attempt.finished_at = now
            attempt.error_type = "workspace_canceled"
            attempt.error_category = "canceled"
        elif attempt.status == WorkspaceAttemptStatus.RUNNING:
            active = True
            attempt.cancel_requested_at = attempt.cancel_requested_at or now
    if active:
        comparison.cancel_requested_at = comparison.cancel_requested_at or now
        comparison.revision += 1
        comparison.updated_at = now
    await database.commit()
    return await comparison_response(database, comparison)
