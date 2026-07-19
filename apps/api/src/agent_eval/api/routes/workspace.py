import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from agent_eval.api.dependencies import (
    DatabaseSession,
    WorkspaceCsrf,
    WorkspaceSession,
)
from agent_eval.api.mutation_guard import sqlite_mutation_guard
from agent_eval.api.schemas.workspace import (
    WorkspaceAgentResponse,
    WorkspaceAttemptAnnotationPatch,
    WorkspaceAttemptError,
    WorkspaceAttemptResponse,
    WorkspaceConversationAgent,
    WorkspaceConversationCreate,
    WorkspaceConversationDetail,
    WorkspaceConversationList,
    WorkspaceConversationPatch,
    WorkspaceConversationResponse,
    WorkspaceMessageResponse,
    WorkspaceRegenerateRequest,
    WorkspaceRegenerateResult,
    WorkspaceSendMessage,
    WorkspaceSendResult,
    WorkspaceTurnResponse,
)
from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    AgentStatus,
    AgentVersionLifecycle,
    WorkspaceAttemptStatus,
    WorkspaceMessageRole,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    Session,
    WorkspaceAgentFavorite,
    WorkspaceAttempt,
    WorkspaceAttemptAnnotation,
    WorkspaceConversation,
    WorkspaceMessage,
)
from agent_eval.workspace.prompt import (
    TranscriptMessage,
    format_workspace_transcript,
    workspace_prompt_limit,
)

router = APIRouter(prefix="/api/v1/workspace", tags=["agent-workspace"])
ACTIVE_ATTEMPT_STATUSES = (
    WorkspaceAttemptStatus.QUEUED,
    WorkspaceAttemptStatus.RUNNING,
)


@asynccontextmanager
async def conversation_mutation_guard(
    request: Request,
    database: DatabaseSession,
    conversation_id: UUID,
) -> AsyncIterator[None]:
    if database.get_bind().dialect.name != "sqlite":
        yield
        return
    locks: dict[UUID, asyncio.Lock] = getattr(
        request.app.state, "workspace_conversation_locks", {}
    )
    request.app.state.workspace_conversation_locks = locks
    lock = locks.setdefault(conversation_id, asyncio.Lock())
    async with lock:
        # Authentication already opened a read transaction. Restart it after waiting so SQLite
        # does not try to upgrade a stale WAL snapshot into a writer.
        await database.rollback()
        yield


def workspace_user_id(auth_session: Session) -> UUID:
    if auth_session.user is None:
        raise RuntimeError("workspace session has no resolved user")
    return auth_session.user.id


def conversation_response(conversation: WorkspaceConversation) -> WorkspaceConversationResponse:
    version = conversation.pinned_agent_version
    agent = version.agent
    return WorkspaceConversationResponse(
        id=conversation.id,
        agent_version_id=conversation.pinned_agent_version_id,
        title=conversation.title,
        last_turn_no=conversation.last_turn_no,
        revision=conversation.revision,
        archived_at=conversation.archived_at,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        agent=WorkspaceConversationAgent(
            agent_id=agent.id,
            agent_version_id=version.id,
            name=agent.name,
            provider=agent.provider,
            version=version.version,
        ),
    )


def message_response(message: WorkspaceMessage) -> WorkspaceMessageResponse:
    return WorkspaceMessageResponse(
        id=message.id,
        turn_no=message.turn_no,
        role=message.role,
        variant_no=message.variant_no,
        active=message.active,
        content=message.content,
        citations=list(message.citations or []),
        created_at=message.created_at,
    )


def attempt_response(
    attempt: WorkspaceAttempt,
    assistant_message: WorkspaceMessage | None = None,
) -> WorkspaceAttemptResponse:
    assistant = assistant_message if assistant_message is not None else attempt.assistant_message
    annotation = attempt.annotation
    return WorkspaceAttemptResponse(
        id=attempt.id,
        conversation_id=attempt.conversation_id,
        user_message_id=attempt.user_message_id,
        assistant_message_id=attempt.assistant_message_id,
        agent_version_id=attempt.agent_version_id,
        generation_no=attempt.generation_no,
        status=attempt.status,
        error_category=attempt.error_category,
        error=(
            WorkspaceAttemptError(
                type=attempt.error_type or "unknown",
                category=attempt.error_category or "unknown",
            )
            if attempt.error_category is not None
            else None
        ),
        assistant_message=message_response(assistant) if assistant is not None else None,
        http_attempt_count=attempt.http_attempt_count,
        last_http_status=attempt.last_http_status,
        latency_ms=attempt.latency_ms,
        input_tokens=attempt.input_tokens,
        output_tokens=attempt.output_tokens,
        total_tokens=attempt.total_tokens,
        feedback=annotation.feedback if annotation is not None else None,
        bookmarked=annotation.bookmarked if annotation is not None else False,
        private_note=annotation.private_note if annotation is not None else None,
        cancel_requested_at=attempt.cancel_requested_at,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        created_at=attempt.created_at,
        updated_at=attempt.updated_at,
    )


def workspace_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


async def owned_conversation(
    database: DatabaseSession,
    conversation_id: UUID,
    owner_user_id: UUID,
    *,
    lock: bool = False,
) -> WorkspaceConversation:
    statement = (
        select(WorkspaceConversation)
        .options(
            joinedload(WorkspaceConversation.pinned_agent_version).joinedload(
                AgentVersion.agent
            )
        )
        .where(
            WorkspaceConversation.id == conversation_id,
            WorkspaceConversation.owner_user_id == owner_user_id,
            WorkspaceConversation.comparison_id.is_(None),
        )
    )
    if lock:
        statement = statement.with_for_update(of=WorkspaceConversation)
    conversation = await database.scalar(statement)
    if conversation is None:
        raise workspace_error(404, "conversation_not_found", "Conversation not found")
    return conversation


async def available_version(
    database: DatabaseSession, version_id: UUID
) -> AgentVersion | None:
    version = await database.scalar(
        select(AgentVersion)
        .options(joinedload(AgentVersion.agent))
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .where(
            AgentVersion.id == version_id,
            AgentVersion.enabled.is_(True),
            AgentVersion.lifecycle_status == AgentVersionLifecycle.ACTIVE,
            Agent.status == AgentStatus.ENABLED,
        )
    )
    return version


def resolve_idempotency_key(body_key: UUID | None, header_key: str | None) -> str:
    parsed_header: UUID | None = None
    if header_key is not None:
        try:
            parsed_header = UUID(header_key)
        except ValueError as error:
            raise workspace_error(
                422, "invalid_idempotency_key", "Invalid idempotency key"
            ) from error
    if body_key is not None and parsed_header is not None and body_key != parsed_header:
        raise workspace_error(422, "idempotency_key_mismatch", "Idempotency keys do not match")
    value = body_key or parsed_header
    if value is None:
        raise workspace_error(422, "idempotency_key_required", "An idempotency key is required")
    return str(value)


def request_fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def active_transcript(
    database: DatabaseSession, conversation_id: UUID, current_turn_no: int
) -> list[TranscriptMessage]:
    messages = list(
        await database.scalars(
            select(WorkspaceMessage)
            .where(
                WorkspaceMessage.conversation_id == conversation_id,
                WorkspaceMessage.turn_no <= current_turn_no,
                WorkspaceMessage.active.is_(True),
            )
            .order_by(WorkspaceMessage.turn_no, WorkspaceMessage.role)
        )
    )
    return [
        TranscriptMessage(
            turn_no=message.turn_no,
            role=message.role,
            content=message.content,
            active=message.active,
        )
        for message in messages
    ]


async def existing_idempotent_attempt(
    database: DatabaseSession,
    *,
    conversation_id: UUID,
    key: str,
    fingerprint: str,
) -> WorkspaceAttempt | None:
    attempt = await database.scalar(
        select(WorkspaceAttempt)
        .options(
            joinedload(WorkspaceAttempt.annotation),
            joinedload(WorkspaceAttempt.assistant_message),
        )
        .where(
            WorkspaceAttempt.conversation_id == conversation_id,
            WorkspaceAttempt.idempotency_key == key,
        )
        .options(joinedload(WorkspaceAttempt.user_message))
    )
    if attempt is not None and attempt.request_fingerprint != fingerprint:
        raise workspace_error(409, "idempotency_conflict", "Idempotency key was already used")
    return attempt


def ensure_mutable_conversation(
    conversation: WorkspaceConversation, expected_revision: int
) -> None:
    if conversation.archived_at is not None:
        raise workspace_error(409, "conversation_archived", "Conversation is archived")
    if conversation.revision != expected_revision:
        raise workspace_error(409, "revision_conflict", "Conversation revision changed")


def ensure_prompt_fits(prompt_characters: int, version: AgentVersion) -> None:
    if prompt_characters > workspace_prompt_limit(version.max_prompt_characters):
        raise workspace_error(422, "prompt_too_long", "Conversation exceeds the Agent prompt limit")


@router.get("/agents", response_model=list[WorkspaceAgentResponse])
async def list_workspace_agents(
    response: Response,
    database: DatabaseSession,
    auth_session: WorkspaceSession,
) -> list[WorkspaceAgentResponse]:
    owner_user_id = workspace_user_id(auth_session)
    rows = list(
        await database.execute(
            select(Agent, AgentVersion, WorkspaceAgentFavorite.owner_user_id)
            .join(AgentVersion, AgentVersion.agent_id == Agent.id)
            .outerjoin(
                WorkspaceAgentFavorite,
                and_(
                    WorkspaceAgentFavorite.agent_id == Agent.id,
                    WorkspaceAgentFavorite.owner_user_id == owner_user_id,
                ),
            )
            .where(
                Agent.status == AgentStatus.ENABLED,
                AgentVersion.enabled.is_(True),
                AgentVersion.lifecycle_status == AgentVersionLifecycle.ACTIVE,
            )
            .order_by(Agent.name, Agent.id, AgentVersion.version.desc())
        )
    )
    response.headers["Cache-Control"] = "private, no-store"
    return [
        WorkspaceAgentResponse(
            agent_id=agent.id,
            agent_version_id=version.id,
            name=agent.name,
            provider=agent.provider,
            description=agent.description,
            logo_url=agent.logo_url,
            version=version.version,
            max_prompt_characters=version.max_prompt_characters,
            supports_citations=version.citations_jsonpath is not None,
            is_favorite=favorite_owner_user_id is not None,
        )
        for agent, version, favorite_owner_user_id in rows
    ]


@router.post(
    "/conversations",
    response_model=WorkspaceConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    payload: WorkspaceConversationCreate,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> WorkspaceConversationResponse:
    version = await available_version(database, payload.agent_version_id)
    if version is None:
        raise workspace_error(409, "agent_version_unavailable", "Agent version is unavailable")
    conversation = WorkspaceConversation(
        owner_user_id=workspace_user_id(auth_session),
        pinned_agent_version_id=payload.agent_version_id,
        pinned_agent_version=version,
        title=payload.title,
    )
    database.add(conversation)
    await database.commit()
    await database.refresh(conversation)
    return conversation_response(conversation)


@router.get("/conversations", response_model=WorkspaceConversationList)
async def list_conversations(
    database: DatabaseSession,
    auth_session: WorkspaceSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    include_archived: bool = True,
) -> WorkspaceConversationList:
    statement = (
        select(WorkspaceConversation)
        .options(
            joinedload(WorkspaceConversation.pinned_agent_version).joinedload(
                AgentVersion.agent
            )
        )
        .where(WorkspaceConversation.owner_user_id == workspace_user_id(auth_session))
        .where(WorkspaceConversation.comparison_id.is_(None))
    )
    if not include_archived:
        statement = statement.where(WorkspaceConversation.archived_at.is_(None))
    conversations = list(
        await database.scalars(
            statement.order_by(
                WorkspaceConversation.updated_at.desc(), WorkspaceConversation.id.desc()
            ).limit(limit)
        )
    )
    return WorkspaceConversationList(
        items=[conversation_response(item) for item in conversations]
    )


@router.get("/conversations/{conversation_id}", response_model=WorkspaceConversationDetail)
async def get_conversation(
    conversation_id: UUID,
    response: Response,
    database: DatabaseSession,
    auth_session: WorkspaceSession,
) -> WorkspaceConversationDetail:
    conversation = await owned_conversation(
        database, conversation_id, workspace_user_id(auth_session)
    )
    messages = list(
        await database.scalars(
            select(WorkspaceMessage)
            .where(WorkspaceMessage.conversation_id == conversation.id)
            .order_by(
                WorkspaceMessage.turn_no,
                WorkspaceMessage.role,
                WorkspaceMessage.variant_no,
            )
        )
    )
    attempts = list(
        await database.scalars(
            select(WorkspaceAttempt)
            .options(
                joinedload(WorkspaceAttempt.annotation),
                joinedload(WorkspaceAttempt.assistant_message),
            )
            .where(WorkspaceAttempt.conversation_id == conversation.id)
            .order_by(WorkspaceAttempt.generation_no)
        )
    )
    attempts_by_user: dict[UUID, list[WorkspaceAttempt]] = {}
    for attempt in attempts:
        attempts_by_user.setdefault(attempt.user_message_id, []).append(attempt)
    turns: list[WorkspaceTurnResponse] = []
    for user_message in (
        message for message in messages if message.role == WorkspaceMessageRole.USER
    ):
        assistants = [
            message
            for message in messages
            if message.turn_no == user_message.turn_no
            and message.role == WorkspaceMessageRole.ASSISTANT
        ]
        turns.append(
            WorkspaceTurnResponse(
                turn_no=user_message.turn_no,
                user_message=message_response(user_message),
                assistant_messages=[message_response(message) for message in assistants],
                attempts=[
                    attempt_response(attempt)
                    for attempt in attempts_by_user.get(user_message.id, [])
                ],
            )
        )
    response.headers["Cache-Control"] = "private, no-store"
    return WorkspaceConversationDetail(
        **conversation_response(conversation).model_dump(), turns=turns
    )


@router.patch(
    "/conversations/{conversation_id}", response_model=WorkspaceConversationResponse
)
async def patch_conversation(
    conversation_id: UUID,
    payload: WorkspaceConversationPatch,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> WorkspaceConversationResponse:
    owner_user_id = workspace_user_id(auth_session)
    async with conversation_mutation_guard(request, database, conversation_id):
        conversation = await owned_conversation(
            database, conversation_id, owner_user_id, lock=True
        )
        if (
            payload.expected_revision is not None
            and conversation.revision != payload.expected_revision
        ):
            raise workspace_error(409, "revision_conflict", "Conversation revision changed")
        if payload.agent_version_id is not None:
            if await database.scalar(
                select(WorkspaceAttempt.id).where(
                    WorkspaceAttempt.conversation_id == conversation.id,
                    WorkspaceAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                )
            ):
                raise workspace_error(
                    409, "attempt_in_progress", "A generation is already active"
                )
            version = await available_version(database, payload.agent_version_id)
            if version is None:
                raise workspace_error(
                    409, "agent_version_unavailable", "Agent version is unavailable"
                )
            if version.agent_id != conversation.pinned_agent_version.agent_id:
                raise workspace_error(
                    409,
                    "agent_identity_mismatch",
                    "Conversation can only use another version of the same Agent",
                )
            conversation.pinned_agent_version = version
            conversation.pinned_agent_version_id = version.id
        if payload.title is not None:
            conversation.title = payload.title
        if payload.archived is not None:
            conversation.archived_at = utc_now() if payload.archived else None
        conversation.revision += 1
        conversation.updated_at = utc_now()
        await database.commit()
        return conversation_response(conversation)


async def queue_attempt(
    *,
    database: DatabaseSession,
    conversation: WorkspaceConversation,
    user_message: WorkspaceMessage,
    version: AgentVersion,
    generation_no: int,
    key: str,
    fingerprint: str,
    transcript: Sequence[TranscriptMessage],
) -> WorkspaceAttempt:
    prompt = format_workspace_transcript(
        list(transcript),
        current_turn_no=user_message.turn_no,
        agent_name=version.agent.name,
        agent_version=version.version,
    )
    ensure_prompt_fits(prompt.characters, version)
    attempt = WorkspaceAttempt(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        agent_version_id=version.id,
        generation_no=generation_no,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        status=WorkspaceAttemptStatus.QUEUED,
        prompt_hash=prompt.sha256,
        prompt_characters=prompt.characters,
        prompt_format=prompt.format_version,
        annotation=None,
    )
    database.add(attempt)
    return attempt


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=WorkspaceSendResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_message(
    conversation_id: UUID,
    payload: WorkspaceSendMessage,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
    idempotency_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WorkspaceSendResult:
    key = resolve_idempotency_key(payload.idempotency_key, idempotency_header)
    fingerprint = request_fingerprint(
        {
            "content": payload.content,
            "conversation_id": str(conversation_id),
            "expected_revision": payload.expected_revision,
            "operation": "send",
        }
    )
    owner_user_id = workspace_user_id(auth_session)
    async with conversation_mutation_guard(request, database, conversation_id):
        return await send_message_locked(
            conversation_id=conversation_id,
            payload=payload,
            database=database,
            owner_user_id=owner_user_id,
            key=key,
            fingerprint=fingerprint,
        )


async def send_message_locked(
    *,
    conversation_id: UUID,
    payload: WorkspaceSendMessage,
    database: DatabaseSession,
    owner_user_id: UUID,
    key: str,
    fingerprint: str,
) -> WorkspaceSendResult:
    conversation = await owned_conversation(database, conversation_id, owner_user_id, lock=True)
    existing = await existing_idempotent_attempt(
        database,
        conversation_id=conversation.id,
        key=key,
        fingerprint=fingerprint,
    )
    if existing is not None:
        return WorkspaceSendResult(
            conversation_revision=conversation.revision,
            user_message=message_response(existing.user_message),
            attempt=attempt_response(existing),
        )
    ensure_mutable_conversation(conversation, payload.expected_revision)
    if await database.scalar(
        select(WorkspaceAttempt.id).where(
            WorkspaceAttempt.conversation_id == conversation.id,
            WorkspaceAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
        )
    ):
        raise workspace_error(409, "attempt_in_progress", "A generation is already active")
    if conversation.last_turn_no:
        previous_answer = await database.scalar(
            select(WorkspaceMessage.id).where(
                WorkspaceMessage.conversation_id == conversation.id,
                WorkspaceMessage.turn_no == conversation.last_turn_no,
                WorkspaceMessage.role == WorkspaceMessageRole.ASSISTANT,
                WorkspaceMessage.active.is_(True),
            )
        )
        if previous_answer is None:
            raise workspace_error(409, "previous_turn_incomplete", "Previous turn is incomplete")
    version = await available_version(database, conversation.pinned_agent_version_id)
    if version is None:
        raise workspace_error(409, "agent_version_unavailable", "Agent version is unavailable")
    turn_no = conversation.last_turn_no + 1
    user_message = WorkspaceMessage(
        id=uuid4(),
        conversation_id=conversation.id,
        turn_no=turn_no,
        role=WorkspaceMessageRole.USER,
        variant_no=1,
        active=True,
        content=payload.content,
    )
    transcript = await active_transcript(database, conversation.id, turn_no)
    transcript.append(
        TranscriptMessage(
            turn_no=turn_no,
            role=WorkspaceMessageRole.USER,
            content=payload.content,
        )
    )
    database.add(user_message)
    attempt = await queue_attempt(
        database=database,
        conversation=conversation,
        user_message=user_message,
        version=version,
        generation_no=1,
        key=key,
        fingerprint=fingerprint,
        transcript=transcript,
    )
    conversation.last_turn_no = turn_no
    conversation.revision += 1
    conversation.updated_at = utc_now()
    try:
        await database.commit()
    except IntegrityError as error:
        await database.rollback()
        duplicate = await existing_idempotent_attempt(
            database,
            conversation_id=conversation_id,
            key=key,
            fingerprint=fingerprint,
        )
        if duplicate is not None:
            duplicate_conversation = await owned_conversation(
                database, conversation_id, owner_user_id
            )
            return WorkspaceSendResult(
                conversation_revision=duplicate_conversation.revision,
                user_message=message_response(duplicate.user_message),
                attempt=attempt_response(duplicate),
            )
        current = await owned_conversation(database, conversation_id, owner_user_id)
        if current.revision != payload.expected_revision:
            raise workspace_error(
                409, "revision_conflict", "Conversation revision changed"
            ) from error
        if await database.scalar(
            select(WorkspaceAttempt.id).where(
                WorkspaceAttempt.conversation_id == conversation_id,
                WorkspaceAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
            )
        ):
            raise workspace_error(
                409, "attempt_in_progress", "A generation is already active"
            ) from error
        raise workspace_error(
            409, "conversation_conflict", "Conversation changed concurrently"
        ) from error
    return WorkspaceSendResult(
        conversation_revision=conversation.revision,
        user_message=message_response(user_message),
        attempt=attempt_response(attempt),
    )


@router.get("/attempts/{attempt_id}", response_model=WorkspaceAttemptResponse)
async def get_attempt(
    attempt_id: UUID,
    response: Response,
    database: DatabaseSession,
    auth_session: WorkspaceSession,
) -> WorkspaceAttemptResponse:
    attempt = await owned_attempt(
        database, attempt_id, workspace_user_id(auth_session)
    )
    response.headers["Cache-Control"] = "private, no-store"
    return attempt_response(attempt)


async def owned_attempt(
    database: DatabaseSession,
    attempt_id: UUID,
    owner_user_id: UUID,
    *,
    lock: bool = False,
) -> WorkspaceAttempt:
    statement = (
        select(WorkspaceAttempt)
        .options(
            joinedload(WorkspaceAttempt.annotation),
            joinedload(WorkspaceAttempt.assistant_message),
        )
        .join(WorkspaceConversation, WorkspaceConversation.id == WorkspaceAttempt.conversation_id)
        .where(
            WorkspaceAttempt.id == attempt_id,
            WorkspaceConversation.owner_user_id == owner_user_id,
        )
    )
    if lock:
        statement = statement.with_for_update(of=WorkspaceAttempt)
    attempt = await database.scalar(statement)
    if attempt is None:
        raise workspace_error(404, "attempt_not_found", "Attempt not found")
    return attempt


@router.patch(
    "/attempts/{attempt_id}/annotation", response_model=WorkspaceAttemptResponse
)
async def patch_attempt_annotation(
    attempt_id: UUID,
    payload: WorkspaceAttemptAnnotationPatch,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> WorkspaceAttemptResponse:
    owner_user_id = workspace_user_id(auth_session)
    async with sqlite_mutation_guard(
        request, database, f"workspace-attempt:{attempt_id}"
    ):
        attempt = await owned_attempt(
            database,
            attempt_id,
            owner_user_id,
            lock=True,
        )
        if attempt.status != WorkspaceAttemptStatus.SUCCEEDED:
            raise workspace_error(
                409,
                "attempt_not_succeeded",
                "Only a successful attempt can be annotated",
            )

        annotation = attempt.annotation
        feedback = annotation.feedback if annotation is not None else None
        bookmarked = annotation.bookmarked if annotation is not None else False
        private_note = annotation.private_note if annotation is not None else None
        if "feedback" in payload.model_fields_set:
            feedback = payload.feedback
        if "bookmarked" in payload.model_fields_set:
            bookmarked = bool(payload.bookmarked)
        if "private_note" in payload.model_fields_set:
            private_note = payload.private_note

        if feedback is None and not bookmarked and private_note is None:
            if annotation is not None:
                attempt.annotation = None
            await database.commit()
            return attempt_response(attempt)

        if annotation is None:
            annotation = WorkspaceAttemptAnnotation(
                attempt_id=attempt.id,
                conversation_id=attempt.conversation_id,
                owner_user_id=owner_user_id,
                feedback=feedback,
                bookmarked=bookmarked,
                private_note=private_note,
            )
            attempt.annotation = annotation
        else:
            annotation.feedback = feedback
            annotation.bookmarked = bookmarked
            annotation.private_note = private_note
            annotation.updated_at = utc_now()
        await database.commit()
        return attempt_response(attempt)


@router.post("/attempts/{attempt_id}/cancel", response_model=WorkspaceAttemptResponse)
async def cancel_attempt(
    attempt_id: UUID,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> WorkspaceAttemptResponse:
    attempt = await owned_attempt(
        database,
        attempt_id,
        workspace_user_id(auth_session),
        lock=True,
    )
    if attempt.status == WorkspaceAttemptStatus.QUEUED:
        now = utc_now()
        attempt.status = WorkspaceAttemptStatus.CANCELED
        attempt.cancel_requested_at = now
        attempt.finished_at = now
        attempt.error_type = "workspace_canceled"
        attempt.error_category = "canceled"
    elif attempt.status == WorkspaceAttemptStatus.RUNNING and attempt.cancel_requested_at is None:
        attempt.cancel_requested_at = utc_now()
    await database.commit()
    return attempt_response(attempt)


@router.post(
    "/conversations/{conversation_id}/messages/{user_message_id}/regenerate",
    response_model=WorkspaceRegenerateResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate_message(
    conversation_id: UUID,
    user_message_id: UUID,
    payload: WorkspaceRegenerateRequest,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
    idempotency_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WorkspaceRegenerateResult:
    owner_user_id = workspace_user_id(auth_session)
    async with conversation_mutation_guard(request, database, conversation_id):
        return await regenerate_message_locked(
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            payload=payload,
            database=database,
            owner_user_id=owner_user_id,
            idempotency_header=idempotency_header,
        )


async def regenerate_message_locked(
    *,
    conversation_id: UUID,
    user_message_id: UUID,
    payload: WorkspaceRegenerateRequest,
    database: DatabaseSession,
    owner_user_id: UUID,
    idempotency_header: str | None,
) -> WorkspaceRegenerateResult:
    key = resolve_idempotency_key(payload.idempotency_key, idempotency_header)
    fingerprint = request_fingerprint(
        {
            "conversation_id": str(conversation_id),
            "expected_revision": payload.expected_revision,
            "operation": "regenerate",
            "user_message_id": str(user_message_id),
        }
    )
    conversation = await owned_conversation(
        database, conversation_id, owner_user_id, lock=True
    )
    existing = await existing_idempotent_attempt(
        database,
        conversation_id=conversation.id,
        key=key,
        fingerprint=fingerprint,
    )
    if existing is not None:
        return WorkspaceRegenerateResult(
            conversation_revision=conversation.revision,
            attempt=attempt_response(existing),
        )
    ensure_mutable_conversation(conversation, payload.expected_revision)
    user_message = await database.scalar(
        select(WorkspaceMessage).where(
            WorkspaceMessage.id == user_message_id,
            WorkspaceMessage.conversation_id == conversation.id,
            WorkspaceMessage.turn_no == conversation.last_turn_no,
            WorkspaceMessage.role == WorkspaceMessageRole.USER,
        )
    )
    if user_message is None:
        raise workspace_error(409, "regeneration_not_latest", "Only the latest turn can regenerate")
    if await database.scalar(
        select(WorkspaceAttempt.id).where(
            WorkspaceAttempt.conversation_id == conversation.id,
            WorkspaceAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
        )
    ):
        raise workspace_error(409, "attempt_in_progress", "A generation is already active")
    version = await available_version(database, conversation.pinned_agent_version_id)
    if version is None:
        raise workspace_error(409, "agent_version_unavailable", "Agent version is unavailable")
    generation_no = int(
        await database.scalar(
            select(func.coalesce(func.max(WorkspaceAttempt.generation_no), 0)).where(
                WorkspaceAttempt.user_message_id == user_message.id
            )
        )
        or 0
    ) + 1
    transcript = await active_transcript(database, conversation.id, user_message.turn_no)
    attempt = await queue_attempt(
        database=database,
        conversation=conversation,
        user_message=user_message,
        version=version,
        generation_no=generation_no,
        key=key,
        fingerprint=fingerprint,
        transcript=transcript,
    )
    conversation.revision += 1
    conversation.updated_at = utc_now()
    try:
        await database.commit()
    except IntegrityError as error:
        await database.rollback()
        duplicate = await existing_idempotent_attempt(
            database,
            conversation_id=conversation_id,
            key=key,
            fingerprint=fingerprint,
        )
        if duplicate is not None:
            duplicate_conversation = await owned_conversation(
                database, conversation_id, owner_user_id
            )
            return WorkspaceRegenerateResult(
                conversation_revision=duplicate_conversation.revision,
                attempt=attempt_response(duplicate),
            )
        raise workspace_error(
            409, "attempt_in_progress", "A generation is already active"
        ) from error
    return WorkspaceRegenerateResult(
        conversation_revision=conversation.revision,
        attempt=attempt_response(attempt),
    )


@router.post(
    "/conversations/{conversation_id}/regenerate",
    response_model=WorkspaceRegenerateResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate_latest_message(
    conversation_id: UUID,
    payload: WorkspaceRegenerateRequest,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
    idempotency_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WorkspaceRegenerateResult:
    conversation = await owned_conversation(
        database, conversation_id, workspace_user_id(auth_session)
    )
    user_message_id = await database.scalar(
        select(WorkspaceMessage.id).where(
            WorkspaceMessage.conversation_id == conversation.id,
            WorkspaceMessage.turn_no == conversation.last_turn_no,
            WorkspaceMessage.role == WorkspaceMessageRole.USER,
        )
    )
    if user_message_id is None:
        raise workspace_error(409, "conversation_empty", "Conversation has no turn to regenerate")
    return await regenerate_message(
        conversation_id,
        user_message_id,
        payload,
        request,
        database,
        auth_session,
        idempotency_header,
    )
