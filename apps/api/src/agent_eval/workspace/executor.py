import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from time import monotonic
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from agent_eval.agents.configuration import (
    AgentConfigurationUnavailable,
    inference_config_for_version,
)
from agent_eval.agents.inference import (
    AttemptCompletion,
    InferenceConfig,
    InferenceOutcome,
    execute_inference,
    execute_streaming_inference,
)
from agent_eval.auth.encryption import TokenCipher
from agent_eval.core.artifact_store import ArtifactStore, StoredArtifact
from agent_eval.core.artifacts import store_workspace_response
from agent_eval.core.settings import Settings
from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    AgentStatus,
    AgentVersionLifecycle,
    ArtifactKind,
    PrivateAgentHealth,
    WorkspaceAttemptStatus,
    WorkspaceMessageRole,
)
from agent_eval.db.models import (
    AgentVersion,
    ArtifactIndex,
    WorkspaceAttempt,
    WorkspaceConversation,
    WorkspaceMessage,
)
from agent_eval.worker.limits import WorkerLimits, agent_request_slots
from agent_eval.workspace.prompt import (
    WORKSPACE_COMPARISON_PROMPT_FORMAT,
    WORKSPACE_PROMPT_FORMAT,
    TranscriptMessage,
    format_workspace_comparison_prompt,
    format_workspace_transcript,
)
from agent_eval.workspace.queue import WorkspaceClaim

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkspaceWork:
    attempt_id: UUID
    conversation_id: UUID
    user_message_id: UUID
    agent_version_id: UUID
    generation_no: int
    concurrency: int
    prompt: str
    config: InferenceConfig


class WorkspaceWorkUnavailable(RuntimeError):
    def __init__(self, error_type: str, category: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.category = category


async def load_workspace_work(
    session_factory: async_sessionmaker[AsyncSession],
    claim: WorkspaceClaim,
    owner_id: str,
    token_cipher: TokenCipher | None,
) -> WorkspaceWork:
    async with session_factory() as session:
        attempt = await session.scalar(
            select(WorkspaceAttempt)
            .options(
                joinedload(WorkspaceAttempt.agent_version).joinedload(AgentVersion.agent),
                joinedload(WorkspaceAttempt.user_message),
                joinedload(WorkspaceAttempt.conversation).joinedload(
                    WorkspaceConversation.comparison
                ),
            )
            .where(
                WorkspaceAttempt.id == claim.attempt_id,
                WorkspaceAttempt.status == WorkspaceAttemptStatus.RUNNING,
                WorkspaceAttempt.claim_owner_id == owner_id,
                WorkspaceAttempt.claim_token == claim.claim_token,
            )
        )
        if attempt is None:
            raise WorkspaceWorkUnavailable("claim_lost", "worker_lost")
        if (
            attempt.user_message.role != WorkspaceMessageRole.USER
            or attempt.user_message.conversation_id != attempt.conversation_id
            or attempt.user_message.variant_no != 1
            or not attempt.user_message.active
        ):
            raise WorkspaceWorkUnavailable("relational_integrity", "internal")
        version = attempt.agent_version
        if (
            not version.enabled
            or version.lifecycle_status != AgentVersionLifecycle.ACTIVE
            or version.agent.status != AgentStatus.ENABLED
        ):
            raise WorkspaceWorkUnavailable("agent_unavailable", "configuration")
        if version.agent.owner_user_id is not None and (
            version.agent.owner_user_id != attempt.conversation.owner_user_id
            or version.private_health_status != PrivateAgentHealth.HEALTHY
        ):
            raise WorkspaceWorkUnavailable("agent_unavailable", "configuration")
        if attempt.prompt_format == WORKSPACE_COMPARISON_PROMPT_FORMAT:
            comparison = attempt.conversation.comparison
            if (
                comparison is None
                or attempt.user_message.turn_no != 1
                or attempt.conversation.last_turn_no != 1
                or attempt.user_message.content != comparison.prompt
            ):
                raise WorkspaceWorkUnavailable("comparison_integrity", "internal")
            formatted = format_workspace_comparison_prompt(comparison.prompt)
        elif attempt.prompt_format == WORKSPACE_PROMPT_FORMAT:
            messages = list(
                await session.scalars(
                    select(WorkspaceMessage)
                    .where(
                        WorkspaceMessage.conversation_id == attempt.conversation_id,
                        WorkspaceMessage.turn_no <= attempt.user_message.turn_no,
                        WorkspaceMessage.active.is_(True),
                    )
                    .order_by(WorkspaceMessage.turn_no, WorkspaceMessage.role)
                )
            )
            formatted = format_workspace_transcript(
                [
                    TranscriptMessage(
                        turn_no=message.turn_no,
                        role=message.role,
                        content=message.content,
                        active=message.active,
                    )
                    for message in messages
                ],
                current_turn_no=attempt.user_message.turn_no,
                agent_name=version.agent.name,
                agent_version=version.version,
            )
        else:
            raise WorkspaceWorkUnavailable("prompt_format_unsupported", "internal")
        if (
            formatted.format_version != attempt.prompt_format
            or formatted.sha256 != attempt.prompt_hash
            or formatted.characters != attempt.prompt_characters
        ):
            raise WorkspaceWorkUnavailable("transcript_changed", "internal")
        try:
            config = inference_config_for_version(
                version, token_cipher, attempt.conversation.owner_user_id
            )
        except AgentConfigurationUnavailable as error:
            raise WorkspaceWorkUnavailable(
                "agent_configuration_unavailable", "configuration"
            ) from error
        return WorkspaceWork(
            attempt_id=attempt.id,
            conversation_id=attempt.conversation_id,
            user_message_id=attempt.user_message_id,
            agent_version_id=attempt.agent_version_id,
            generation_no=attempt.generation_no,
            concurrency=version.concurrency,
            prompt=formatted.content,
            config=config,
        )


async def start_http_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    claim: WorkspaceClaim,
    owner_id: str,
    attempt_no: int,
) -> UUID | None:
    async with session_factory() as session:
        result = await session.execute(
            update(WorkspaceAttempt)
            .where(
                WorkspaceAttempt.id == claim.attempt_id,
                WorkspaceAttempt.status == WorkspaceAttemptStatus.RUNNING,
                WorkspaceAttempt.claim_owner_id == owner_id,
                WorkspaceAttempt.claim_token == claim.claim_token,
                WorkspaceAttempt.cancel_requested_at.is_(None),
                WorkspaceAttempt.http_attempt_count == attempt_no - 1,
            )
            .values(http_attempt_count=attempt_no, updated_at=utc_now())
        )
        await session.commit()
        return uuid4() if getattr(result, "rowcount", 0) == 1 else None


async def finish_http_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    claim: WorkspaceClaim,
    owner_id: str,
    completion: AttemptCompletion,
) -> None:
    values: dict[str, object] = {
        "last_http_status": completion.status_code,
        "latency_ms": func.coalesce(WorkspaceAttempt.latency_ms, 0) + completion.latency_ms,
        "updated_at": utc_now(),
    }
    if completion.input_tokens is not None:
        values["input_tokens"] = completion.input_tokens
    if completion.output_tokens is not None:
        values["output_tokens"] = completion.output_tokens
    if completion.total_tokens is not None:
        values["total_tokens"] = completion.total_tokens
    async with session_factory() as session:
        await session.execute(
            update(WorkspaceAttempt)
            .where(
                WorkspaceAttempt.id == claim.attempt_id,
                WorkspaceAttempt.status == WorkspaceAttemptStatus.RUNNING,
                WorkspaceAttempt.claim_owner_id == owner_id,
                WorkspaceAttempt.claim_token == claim.claim_token,
            )
            .values(**values)
        )
        await session.commit()


PARTIAL_ANSWER_WRITE_INTERVAL_SECONDS = 0.5


async def write_partial_answer(
    session_factory: async_sessionmaker[AsyncSession],
    claim: WorkspaceClaim,
    owner_id: str,
    text: str,
) -> None:
    """Persist the latest streaming partial answer if this worker still owns the claim."""
    async with session_factory() as session:
        await session.execute(
            update(WorkspaceAttempt)
            .where(
                WorkspaceAttempt.id == claim.attempt_id,
                WorkspaceAttempt.status == WorkspaceAttemptStatus.RUNNING,
                WorkspaceAttempt.claim_owner_id == owner_id,
                WorkspaceAttempt.claim_token == claim.claim_token,
            )
            .values(partial_answer=text, updated_at=utc_now())
        )
        await session.commit()


class PartialAnswerWriter:
    """Throttled sink for streaming partial answers (~one DB write per interval).

    The leading write goes through immediately; later deltas are coalesced so a
    fast token stream cannot flood the database. flush() persists the trailing
    state so stream consumers see the complete text before finalization.
    """

    def __init__(
        self,
        persist: Callable[[str], Awaitable[None]],
        *,
        interval: float = PARTIAL_ANSWER_WRITE_INTERVAL_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._persist = persist
        self._interval = interval
        self._clock = clock
        self._latest: str | None = None
        self._last_write: float | None = None
        self._pending = False

    async def write(self, text: str) -> None:
        self._latest = text
        now = self._clock()
        if self._last_write is not None and now - self._last_write < self._interval:
            self._pending = True
            return
        self._last_write = now
        self._pending = False
        await self._persist(text)

    async def flush(self) -> None:
        if self._pending and self._latest is not None:
            self._pending = False
            self._last_write = self._clock()
            await self._persist(self._latest)


async def watch_cancellation(
    session_factory: async_sessionmaker[AsyncSession],
    claim: WorkspaceClaim,
    owner_id: str,
    target: asyncio.Task[InferenceOutcome],
    requested: asyncio.Event,
) -> None:
    while not target.done():
        await asyncio.sleep(0.2)
        async with session_factory() as session:
            state = (
                await session.execute(
                    select(
                        WorkspaceAttempt.status,
                        WorkspaceAttempt.claim_owner_id,
                        WorkspaceAttempt.claim_token,
                        WorkspaceAttempt.cancel_requested_at,
                    ).where(WorkspaceAttempt.id == claim.attempt_id)
                )
            ).one_or_none()
        if (
            state is None
            or state.status != WorkspaceAttemptStatus.RUNNING
            or state.claim_owner_id != owner_id
            or state.claim_token != claim.claim_token
        ):
            target.cancel()
            return
        if state.cancel_requested_at is not None:
            requested.set()
            target.cancel()
            return


def failed_outcome(error_type: str) -> InferenceOutcome:
    return InferenceOutcome(
        success=False,
        canceled=False,
        answer=None,
        citations=[],
        response_content=None,
        latency_ms=0,
        attempt_count=0,
        error_type=error_type,
    )


def safe_error_category(error_type: str | None) -> str:
    if error_type is None:
        return "unknown"
    return error_type.removeprefix("agent_").removeprefix("workspace_")[:80]


async def stored_artifact_is_indexed(
    session_factory: async_sessionmaker[AsyncSession],
    claim: WorkspaceClaim,
    stored: StoredArtifact,
) -> bool | None:
    try:
        async with session_factory() as session:
            indexed = await session.scalar(
                select(ArtifactIndex.id)
                .join(WorkspaceAttempt, WorkspaceAttempt.artifact_id == ArtifactIndex.id)
                .where(
                    WorkspaceAttempt.id == claim.attempt_id,
                    ArtifactIndex.relative_path == stored.relative_path,
                )
            )
        return indexed is not None
    except Exception as error:
        logger.error(
            "workspace_artifact_index_check_failed",
            extra={
                "attempt_id": claim.attempt_id,
                "error_type": type(error).__name__,
            },
        )
        return None


async def best_effort_delete_artifact(
    artifact_store: ArtifactStore,
    claim: WorkspaceClaim,
    stored: StoredArtifact,
) -> None:
    delete_task = asyncio.create_task(
        asyncio.to_thread(artifact_store.delete, stored.relative_path),
        name=f"workspace-artifact-delete-{claim.attempt_id}",
    )
    try:
        await asyncio.shield(delete_task)
    except asyncio.CancelledError:
        await asyncio.gather(delete_task, return_exceptions=True)
    except Exception as error:
        logger.error(
            "workspace_artifact_delete_failed",
            extra={
                "attempt_id": claim.attempt_id,
                "error_type": type(error).__name__,
            },
        )


async def delete_artifact_if_unindexed(
    session_factory: async_sessionmaker[AsyncSession],
    artifact_store: ArtifactStore,
    claim: WorkspaceClaim,
    stored: StoredArtifact,
) -> None:
    indexed = await stored_artifact_is_indexed(session_factory, claim, stored)
    if indexed is False:
        await best_effort_delete_artifact(artifact_store, claim, stored)


async def finalize_workspace_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    claim: WorkspaceClaim,
    owner_id: str,
    outcome: InferenceOutcome,
    stored: StoredArtifact | None,
    *,
    forced_category: str | None = None,
) -> bool:
    async with session_factory() as session, session.begin():
        attempt = await session.scalar(
            select(WorkspaceAttempt)
            .where(WorkspaceAttempt.id == claim.attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            attempt is None
            or attempt.status != WorkspaceAttemptStatus.RUNNING
            or attempt.claim_owner_id != owner_id
            or attempt.claim_token != claim.claim_token
        ):
            return False
        now = utc_now()
        if stored is not None:
            artifact = ArtifactIndex(
                kind=ArtifactKind.AGENT_RESPONSE,
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                expires_at=now + timedelta(days=settings.artifact_retention_days),
            )
            session.add(artifact)
            await session.flush()
            attempt.artifact_id = artifact.id

        canceled = attempt.cancel_requested_at is not None or outcome.canceled
        if canceled:
            attempt.status = WorkspaceAttemptStatus.CANCELED
            attempt.error_type = "workspace_canceled"
            attempt.error_category = "canceled"
            attempt.cancel_requested_at = attempt.cancel_requested_at or now
        elif outcome.success and outcome.answer is not None:
            user_message = await session.get(WorkspaceMessage, attempt.user_message_id)
            if (
                user_message is None
                or user_message.role != WorkspaceMessageRole.USER
                or user_message.conversation_id != attempt.conversation_id
                or user_message.variant_no != 1
                or not user_message.active
            ):
                raise RuntimeError("workspace attempt message relationship is invalid")
            await session.execute(
                update(WorkspaceMessage)
                .where(
                    WorkspaceMessage.conversation_id == attempt.conversation_id,
                    WorkspaceMessage.turn_no == user_message.turn_no,
                    WorkspaceMessage.role == WorkspaceMessageRole.ASSISTANT,
                    WorkspaceMessage.active.is_(True),
                )
                .values(active=False)
            )
            assistant = WorkspaceMessage(
                conversation_id=attempt.conversation_id,
                turn_no=user_message.turn_no,
                role=WorkspaceMessageRole.ASSISTANT,
                variant_no=attempt.generation_no,
                active=True,
                content=outcome.answer,
                citations=outcome.citations,
            )
            session.add(assistant)
            await session.flush()
            attempt.assistant_message_id = assistant.id
            attempt.status = WorkspaceAttemptStatus.SUCCEEDED
            attempt.error_type = None
            attempt.error_category = None
        else:
            error_type = outcome.error_type or "workspace_unknown"
            attempt.status = WorkspaceAttemptStatus.FAILED
            attempt.error_type = error_type[:80]
            attempt.error_category = forced_category or safe_error_category(error_type)
            if attempt.last_http_status in {401, 402, 403}:
                attempt.claim_owner_id = None
                attempt.claim_token = None
                attempt.lease_expires_at = None
                attempt.heartbeat_at = None
                attempt.finished_at = now
                attempt.updated_at = now
                with session.no_autoflush:
                    version = await session.get(AgentVersion, attempt.agent_version_id)
                if version is not None and version.private_preset is not None:
                    health_error = (
                        "balance_exhausted"
                        if attempt.last_http_status == 402
                        else "authentication_failed"
                    )
                    version.lifecycle_status = AgentVersionLifecycle.DRAFT
                    version.enabled = False
                    version.private_health_status = PrivateAgentHealth.UNHEALTHY
                    version.private_health_checked_at = now
                    version.private_health_error_code = health_error
                    version.private_health_latency_ms = max(0, outcome.latency_ms)
                    await session.execute(
                        update(WorkspaceAttempt)
                        .where(
                            WorkspaceAttempt.id != attempt.id,
                            WorkspaceAttempt.agent_version_id == version.id,
                            WorkspaceAttempt.status == WorkspaceAttemptStatus.QUEUED,
                        )
                        .values(
                            status=WorkspaceAttemptStatus.CANCELED,
                            cancel_requested_at=now,
                            finished_at=now,
                            error_type="workspace_canceled",
                            error_category="canceled",
                            updated_at=now,
                        )
                    )
                    await session.execute(
                        update(WorkspaceAttempt)
                        .where(
                            WorkspaceAttempt.id != attempt.id,
                            WorkspaceAttempt.agent_version_id == version.id,
                            WorkspaceAttempt.status == WorkspaceAttemptStatus.RUNNING,
                        )
                        .values(cancel_requested_at=now, updated_at=now)
                    )

        if outcome.latency_ms or attempt.latency_ms is None:
            attempt.latency_ms = outcome.latency_ms
        attempt.http_attempt_count = max(attempt.http_attempt_count, outcome.attempt_count)
        # Terminal answers live on the assistant message (or the error fields);
        # the streaming partial is cleared so no stale in-progress text remains.
        attempt.partial_answer = None
        attempt.claim_owner_id = None
        attempt.claim_token = None
        attempt.lease_expires_at = None
        attempt.heartbeat_at = None
        attempt.finished_at = now
        attempt.updated_at = now
        conversation = await session.get(WorkspaceConversation, attempt.conversation_id)
        if conversation is not None:
            conversation.updated_at = now
        return True


async def process_workspace_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    claim: WorkspaceClaim,
    *,
    artifact_store: ArtifactStore,
    token_cipher: TokenCipher | None,
    owner_id: str,
    limits: WorkerLimits,
    transport: httpx.AsyncBaseTransport | None = None,
    partial_write_interval: float = PARTIAL_ANSWER_WRITE_INTERVAL_SECONDS,
) -> bool:
    stored: StoredArtifact | None = None
    try:
        work = await load_workspace_work(session_factory, claim, owner_id, token_cipher)

        async def execute() -> InferenceOutcome:
            async with (
                agent_request_slots(
                    limits.agent_versions,
                    limits.agent_requests,
                    version_id=str(work.agent_version_id),
                    version_limit=work.concurrency,
                ),
                httpx.AsyncClient(transport=transport, follow_redirects=False) as client,
            ):
                if work.config.supports_streaming:
                    writer = PartialAnswerWriter(
                        partial(write_partial_answer, session_factory, claim, owner_id),
                        interval=partial_write_interval,
                    )
                    try:
                        return await execute_streaming_inference(
                            client,
                            work.config,
                            work.prompt,
                            previous_attempt_count=0,
                            start_attempt=partial(
                                start_http_attempt,
                                session_factory,
                                claim,
                                owner_id,
                            ),
                            finish_attempt=partial(
                                finish_http_attempt,
                                session_factory,
                                claim,
                                owner_id,
                            ),
                            on_partial=writer.write,
                        )
                    finally:
                        # Best-effort trailing write; a canceled task may not
                        # complete it, which finalize then clears anyway.
                        with suppress(Exception):
                            await writer.flush()
                return await execute_inference(
                    client,
                    work.config,
                    work.prompt,
                    previous_attempt_count=0,
                    start_attempt=partial(
                        start_http_attempt,
                        session_factory,
                        claim,
                        owner_id,
                    ),
                    finish_attempt=partial(
                        finish_http_attempt,
                        session_factory,
                        claim,
                        owner_id,
                    ),
                )

        inference_task = asyncio.create_task(
            execute(), name=f"workspace-inference-{claim.attempt_id}"
        )
        cancellation_requested = asyncio.Event()
        watcher = asyncio.create_task(
            watch_cancellation(
                session_factory,
                claim,
                owner_id,
                inference_task,
                cancellation_requested,
            ),
            name=f"workspace-cancel-watch-{claim.attempt_id}",
        )
        try:
            outcome = await inference_task
        except asyncio.CancelledError:
            if not cancellation_requested.is_set():
                raise
            outcome = InferenceOutcome(
                success=False,
                canceled=True,
                answer=None,
                citations=[],
                response_content=None,
                latency_ms=0,
                attempt_count=0,
                error_type="workspace_canceled",
            )
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

        if outcome.response_content is not None:
            storage_task = asyncio.create_task(
                asyncio.to_thread(
                    store_workspace_response,
                    artifact_store,
                    conversation_id=work.conversation_id,
                    attempt_id=work.attempt_id,
                    content=outcome.response_content,
                ),
                name=f"workspace-artifact-store-{claim.attempt_id}",
            )
            try:
                stored = await asyncio.shield(storage_task)
            except asyncio.CancelledError:
                with suppress(Exception):
                    stored = await storage_task
                raise
        finalized = await finalize_workspace_attempt(
            session_factory,
            settings,
            claim,
            owner_id,
            outcome,
            stored,
        )
        if not finalized and stored is not None:
            await delete_artifact_if_unindexed(
                session_factory, artifact_store, claim, stored
            )
        return finalized
    except asyncio.CancelledError:
        if stored is not None:
            await delete_artifact_if_unindexed(
                session_factory, artifact_store, claim, stored
            )
        raise
    except WorkspaceWorkUnavailable as error:
        if error.error_type == "claim_lost":
            return False
        return await finalize_workspace_attempt(
            session_factory,
            settings,
            claim,
            owner_id,
            failed_outcome(error.error_type),
            None,
            forced_category=error.category,
        )
    except Exception as error:
        logger.error(
            "workspace_attempt_processing_failed",
            extra={
                "attempt_id": claim.attempt_id,
                "error_type": type(error).__name__,
            },
        )
        if stored is not None:
            await delete_artifact_if_unindexed(
                session_factory, artifact_store, claim, stored
            )
        return await finalize_workspace_attempt(
            session_factory,
            settings,
            claim,
            owner_id,
            failed_outcome("workspace_internal"),
            None,
            forced_category="internal",
        )
