from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from agent_eval.agents.prompt import PromptBuildError, build_question_prompt
from agent_eval.api.dependencies import (
    DatabaseSession,
    OperatorCsrfSession,
    OperatorSession,
)
from agent_eval.api.mutation_guard import sqlite_mutation_guard
from agent_eval.api.routes.workspace import request_fingerprint, workspace_error, workspace_user_id
from agent_eval.api.schemas.evaluation_cases import (
    EvaluationCaseCreate,
    EvaluationCaseList,
    EvaluationCasePatch,
    EvaluationCasePublish,
    EvaluationCaseResponse,
    EvaluationCaseReview,
    EvaluationCaseSource,
)
from agent_eval.db.base import utc_now
from agent_eval.db.enums import Ability, EvaluationCaseStatus, WorkspaceAttemptStatus
from agent_eval.db.models import (
    AgentVersion,
    EvaluationCase,
    Question,
    WorkspaceAttempt,
    WorkspaceConversation,
)
from agent_eval.evaluation_cases.service import (
    canonical_sha256,
    normalized_prompt_hash,
    record_hash,
    source_payload,
)

router = APIRouter(prefix="/api/v1/evaluation-cases", tags=["evaluation-cases"])


async def owned_case(
    database: AsyncSession, case_id: UUID, owner_user_id: UUID, *, lock: bool = False
) -> EvaluationCase:
    statement = select(EvaluationCase).where(
        EvaluationCase.id == case_id,
        EvaluationCase.owner_user_id == owner_user_id,
    )
    if lock:
        statement = statement.with_for_update(of=EvaluationCase)
    case = await database.scalar(statement)
    if case is None:
        raise workspace_error(404, "evaluation_case_not_found", "Evaluation case not found")
    return case


def case_response(case: EvaluationCase) -> EvaluationCaseResponse:
    return EvaluationCaseResponse(
        id=case.id,
        status=case.status,
        revision=case.revision,
        prompt=case.prompt,
        reference_answer=case.reference_answer,
        language=case.language,
        ability=case.ability,
        question_type=case.question_type,
        requires_citations=case.requires_citations,
        difficulty=case.difficulty,
        domain=case.domain,
        review_notes=case.review_notes,
        standalone_verified=case.standalone_verified,
        reference_answer_verified=case.reference_answer_verified,
        rights_verified=case.rights_verified,
        sensitive_data_verified=case.sensitive_data_verified,
        source_hash=case.source_hash,
        record_hash=case.record_hash,
        reviewed_at=case.reviewed_at,
        published_at=case.published_at,
        created_at=case.created_at,
        updated_at=case.updated_at,
        source=EvaluationCaseSource.model_validate(case.source_payload_json),
    )


async def source_attempt(
    database: AsyncSession, attempt_id: UUID, owner_user_id: UUID
) -> WorkspaceAttempt:
    attempt = await database.scalar(
        select(WorkspaceAttempt)
        .options(
            joinedload(WorkspaceAttempt.assistant_message),
            joinedload(WorkspaceAttempt.user_message),
            joinedload(WorkspaceAttempt.agent_version).joinedload(AgentVersion.agent),
            joinedload(WorkspaceAttempt.artifact),
            joinedload(WorkspaceAttempt.conversation),
        )
        .join(WorkspaceConversation, WorkspaceConversation.id == WorkspaceAttempt.conversation_id)
        .where(
            WorkspaceAttempt.id == attempt_id,
            WorkspaceConversation.owner_user_id == owner_user_id,
        )
    )
    if attempt is None:
        raise workspace_error(404, "attempt_not_found", "Attempt not found")
    if attempt.status != WorkspaceAttemptStatus.SUCCEEDED or attempt.assistant_message is None:
        raise workspace_error(
            409, "attempt_not_succeeded", "Only a successful answer can become a draft"
        )
    return attempt


@router.post("", response_model=EvaluationCaseResponse, status_code=status.HTTP_201_CREATED)
async def create_case(
    payload: EvaluationCaseCreate,
    request: Request,
    database: DatabaseSession,
    auth_session: OperatorCsrfSession,
) -> EvaluationCaseResponse:
    user = auth_session.user
    if user is None or user.is_shared:
        raise workspace_error(
            403,
            "named_operator_required",
            "A named operator account is required to curate evaluation cases",
        )
    owner_user_id = user.id
    guard_key = f"evaluation-case-create:{owner_user_id}"
    async with sqlite_mutation_guard(request, database, guard_key):
        return await create_case_locked(payload, database, owner_user_id)


async def create_case_locked(
    payload: EvaluationCaseCreate,
    database: AsyncSession,
    owner_user_id: UUID,
) -> EvaluationCaseResponse:
    if payload.idempotency_key is None:
        raise workspace_error(
            422, "idempotency_key_required", "An idempotency key is required"
        )
    key = str(payload.idempotency_key)
    fingerprint = request_fingerprint(
        {
            "operation": "create_evaluation_case",
            "source_attempt_id": str(payload.source_attempt_id),
        }
    )
    existing = await database.scalar(
        select(EvaluationCase)
        .where(
            EvaluationCase.owner_user_id == owner_user_id,
            EvaluationCase.idempotency_key == key,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise workspace_error(
                409, "idempotency_conflict", "Idempotency key was already used"
            )
        return case_response(existing)
    previous = await database.scalar(
        select(EvaluationCase)
        .where(
            EvaluationCase.owner_user_id == owner_user_id,
            EvaluationCase.source_attempt_id == payload.source_attempt_id,
        )
    )
    if previous is not None:
        raise workspace_error(
            409, "source_already_curated", "This answer already has an evaluation case"
        )
    attempt = await source_attempt(database, payload.source_attempt_id, owner_user_id)
    snapshot = source_payload(attempt)
    case = EvaluationCase(
        owner_user_id=owner_user_id,
        source_attempt=attempt,
        source_conversation_id=attempt.conversation_id,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        source_hash=canonical_sha256(snapshot),
        source_payload_json=snapshot,
        status=EvaluationCaseStatus.DRAFT,
        revision=0,
        prompt=attempt.user_message.content,
    )
    database.add(case)
    try:
        await database.commit()
    except IntegrityError as error:
        await database.rollback()
        duplicate = await database.scalar(
            select(EvaluationCase)
            .where(
                EvaluationCase.owner_user_id == owner_user_id,
                EvaluationCase.idempotency_key == key,
            )
        )
        if duplicate is not None and duplicate.request_fingerprint == fingerprint:
            return case_response(duplicate)
        raise workspace_error(
            409, "evaluation_case_conflict", "Evaluation case changed concurrently"
        ) from error
    return case_response(case)


@router.get("", response_model=EvaluationCaseList)
async def list_cases(
    database: DatabaseSession,
    auth_session: OperatorSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> EvaluationCaseList:
    cases = list(
        await database.scalars(
            select(EvaluationCase)
            .where(EvaluationCase.owner_user_id == workspace_user_id(auth_session))
            .order_by(EvaluationCase.updated_at.desc(), EvaluationCase.id.desc())
            .limit(limit)
        )
    )
    return EvaluationCaseList(items=[case_response(case) for case in cases])


@router.get("/{case_id}", response_model=EvaluationCaseResponse)
async def get_case(
    case_id: UUID,
    database: DatabaseSession,
    auth_session: OperatorSession,
) -> EvaluationCaseResponse:
    return case_response(
        await owned_case(database, case_id, workspace_user_id(auth_session))
    )


def ensure_case_revision(case: EvaluationCase, expected_revision: int) -> None:
    if case.revision != expected_revision:
        raise workspace_error(409, "revision_conflict", "Evaluation case revision changed")


def reset_review(case: EvaluationCase) -> None:
    case.status = EvaluationCaseStatus.DRAFT
    case.standalone_verified = False
    case.reference_answer_verified = False
    case.rights_verified = False
    case.sensitive_data_verified = False
    case.record_hash = None
    case.reviewed_by_user_id = None
    case.reviewed_at = None
    case.published_by_user_id = None
    case.published_at = None
    case.published_prompt_hash = None


@router.patch("/{case_id}", response_model=EvaluationCaseResponse)
async def patch_case(
    case_id: UUID,
    payload: EvaluationCasePatch,
    request: Request,
    database: DatabaseSession,
    auth_session: OperatorCsrfSession,
) -> EvaluationCaseResponse:
    owner_user_id = workspace_user_id(auth_session)
    async with sqlite_mutation_guard(request, database, f"evaluation-case:{case_id}"):
        return await patch_case_locked(case_id, payload, database, owner_user_id)


async def patch_case_locked(
    case_id: UUID,
    payload: EvaluationCasePatch,
    database: AsyncSession,
    owner_user_id: UUID,
) -> EvaluationCaseResponse:
    case = await owned_case(database, case_id, owner_user_id, lock=True)
    if case.status == EvaluationCaseStatus.PUBLISHED:
        raise workspace_error(409, "evaluation_case_published", "Published cases are immutable")
    ensure_case_revision(case, payload.expected_revision)
    editable_fields = {
        "prompt",
        "reference_answer",
        "language",
        "ability",
        "question_type",
        "requires_citations",
        "difficulty",
        "domain",
        "review_notes",
    }
    for field in payload.model_fields_set & editable_fields:
        value = getattr(payload, field)
        if field == "prompt" and value is None:
            raise workspace_error(422, "prompt_required", "Prompt cannot be blank")
        if field in {"question_type", "requires_citations"} and value is None:
            raise workspace_error(422, "field_required", f"{field} cannot be null")
        setattr(case, field, value)
    if case.status == EvaluationCaseStatus.REVIEWED:
        reset_review(case)
    case.revision += 1
    case.updated_at = utc_now()
    await database.commit()
    return case_response(case)


@router.post("/{case_id}/review", response_model=EvaluationCaseResponse)
async def review_case(
    case_id: UUID,
    payload: EvaluationCaseReview,
    request: Request,
    database: DatabaseSession,
    auth_session: OperatorCsrfSession,
) -> EvaluationCaseResponse:
    owner_user_id = workspace_user_id(auth_session)
    async with sqlite_mutation_guard(request, database, f"evaluation-case:{case_id}"):
        return await review_case_locked(case_id, payload, database, owner_user_id)


async def review_case_locked(
    case_id: UUID,
    payload: EvaluationCaseReview,
    database: AsyncSession,
    owner_user_id: UUID,
) -> EvaluationCaseResponse:
    case = await owned_case(database, case_id, owner_user_id, lock=True)
    if case.status == EvaluationCaseStatus.PUBLISHED:
        raise workspace_error(409, "evaluation_case_published", "Published cases are immutable")
    ensure_case_revision(case, payload.expected_revision)
    if case.reference_answer is None or case.language is None or case.ability is None:
        raise workspace_error(
            422,
            "evaluation_case_incomplete",
            "Prompt, reference answer, language, and ability are required",
        )
    if canonical_sha256(case.source_payload_json) != case.source_hash:
        raise workspace_error(409, "evaluation_case_source_changed", "Source evidence changed")
    if case.ability == Ability.RETRIEVAL and not case.requires_citations:
        raise workspace_error(
            422, "citations_required", "Retrieval cases must require citations"
        )
    try:
        build_question_prompt(
            question_type=case.question_type,
            prompt=case.prompt,
            choices=None,
            requires_citations=case.requires_citations,
            ability=case.ability,
        )
    except PromptBuildError as error:
        raise workspace_error(422, "evaluation_case_invalid", str(error)) from error
    case.standalone_verified = payload.standalone_verified
    case.reference_answer_verified = payload.reference_answer_verified
    case.rights_verified = payload.rights_verified
    case.sensitive_data_verified = payload.sensitive_data_verified
    case.record_hash = record_hash(case)
    case.reviewed_by_user_id = owner_user_id
    case.reviewed_at = utc_now()
    case.status = EvaluationCaseStatus.REVIEWED
    case.revision += 1
    case.updated_at = utc_now()
    await database.commit()
    return case_response(case)


@router.post("/{case_id}/publish", response_model=EvaluationCaseResponse)
async def publish_case(
    case_id: UUID,
    payload: EvaluationCasePublish,
    request: Request,
    database: DatabaseSession,
    auth_session: OperatorCsrfSession,
) -> EvaluationCaseResponse:
    user = auth_session.user
    if user is None or user.is_shared:
        raise workspace_error(
            403, "named_operator_required", "A named operator account is required to publish"
        )
    owner_user_id = user.id
    async with sqlite_mutation_guard(request, database, f"evaluation-case:{case_id}"):
        return await publish_case_locked(case_id, payload, database, owner_user_id)


async def publish_case_locked(
    case_id: UUID,
    payload: EvaluationCasePublish,
    database: AsyncSession,
    owner_user_id: UUID,
) -> EvaluationCaseResponse:
    case = await owned_case(database, case_id, owner_user_id, lock=True)
    ensure_case_revision(case, payload.expected_revision)
    if case.status != EvaluationCaseStatus.REVIEWED or case.record_hash is None:
        raise workspace_error(
            409, "evaluation_case_not_reviewed", "Review the case before publishing"
        )
    if record_hash(case) != case.record_hash:
        raise workspace_error(409, "evaluation_case_changed", "Reviewed content changed")
    if canonical_sha256(case.source_payload_json) != case.source_hash:
        raise workspace_error(409, "evaluation_case_source_changed", "Source evidence changed")
    prompt_hash = normalized_prompt_hash(case.prompt)
    formal_prompts = list(await database.scalars(select(Question.prompt)))
    if any(normalized_prompt_hash(prompt) == prompt_hash for prompt in formal_prompts):
        raise workspace_error(
            409, "published_prompt_duplicate", "This prompt already exists in a formal dataset"
        )
    case.status = EvaluationCaseStatus.PUBLISHED
    case.published_by_user_id = owner_user_id
    case.published_at = utc_now()
    case.published_prompt_hash = prompt_hash
    case.revision += 1
    case.updated_at = utc_now()
    try:
        await database.commit()
    except IntegrityError as error:
        await database.rollback()
        raise workspace_error(
            409, "published_prompt_duplicate", "This prompt is already published"
        ) from error
    return case_response(case)
