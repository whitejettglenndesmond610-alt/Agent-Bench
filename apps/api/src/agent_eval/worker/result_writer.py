import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_eval.agents.inference import AttemptCompletion, InferenceOutcome
from agent_eval.core.artifact_store import ArtifactStore
from agent_eval.core.artifacts import store_agent_response, store_trajectory
from agent_eval.core.settings import Settings
from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    ArtifactKind,
    AttemptStatus,
    DesiredRunState,
    ExecutionStatus,
    RunAgentStatus,
    RunStage,
    RunStatus,
)
from agent_eval.db.models import (
    ArtifactIndex,
    BadPatternRule,
    CitationRecord,
    EvaluationRun,
    ExecutionItem,
    HttpAttempt,
    Question,
    RunAgent,
)
from agent_eval.quality.detector import scan_bad_patterns
from agent_eval.runs.queue import lock_owned_run


@dataclass(frozen=True)
class StartAttemptCommand:
    item_id: UUID
    attempt_no: int
    future: asyncio.Future[UUID | None]


@dataclass(frozen=True)
class FinishAttemptCommand:
    completion: AttemptCompletion
    future: asyncio.Future[None]


@dataclass(frozen=True)
class FinalizeItemCommand:
    item_id: UUID
    run_agent_id: UUID
    agent_id: UUID
    question_id: UUID
    outcome: InferenceOutcome
    future: asyncio.Future[None]
    task_reward: Decimal | None = None
    trajectory: dict[str, Any] | None = None
    simulator_input_tokens: int | None = None
    simulator_output_tokens: int | None = None


@dataclass(frozen=True)
class FinishInferenceCommand:
    future: asyncio.Future[bool]


@dataclass(frozen=True)
class StopCommand:
    future: asyncio.Future[None]


WriterCommand = (
    StartAttemptCommand
    | FinishAttemptCommand
    | FinalizeItemCommand
    | FinishInferenceCommand
    | StopCommand
)


class ResultWriter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        run_id: UUID,
        owner_id: str,
        artifact_store: ArtifactStore,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._run_id = run_id
        self._owner_id = owner_id
        self._artifact_store = artifact_store
        self._queue: asyncio.Queue[WriterCommand] = asyncio.Queue()
        self._bad_pattern_rules: list[BadPatternRule] | None = None

    async def start_attempt(self, item_id: UUID, attempt_no: int) -> UUID | None:
        future: asyncio.Future[UUID | None] = asyncio.get_running_loop().create_future()
        await self._queue.put(StartAttemptCommand(item_id, attempt_no, future))
        return await future

    async def finish_attempt(self, completion: AttemptCompletion) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put(FinishAttemptCommand(completion, future))
        await future

    async def finalize_item(
        self,
        *,
        item_id: UUID,
        run_agent_id: UUID,
        agent_id: UUID,
        question_id: UUID,
        outcome: InferenceOutcome,
        task_reward: Decimal | None = None,
        trajectory: dict[str, Any] | None = None,
        simulator_input_tokens: int | None = None,
        simulator_output_tokens: int | None = None,
    ) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put(
            FinalizeItemCommand(
                item_id=item_id,
                run_agent_id=run_agent_id,
                agent_id=agent_id,
                question_id=question_id,
                outcome=outcome,
                future=future,
                task_reward=task_reward,
                trajectory=trajectory,
                simulator_input_tokens=simulator_input_tokens,
                simulator_output_tokens=simulator_output_tokens,
            )
        )
        await future

    async def finish_inference(self) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        await self._queue.put(FinishInferenceCommand(future))
        return await future

    async def stop(self) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put(StopCommand(future))
        await future

    async def run(self) -> None:
        async with self._session_factory() as session:
            while True:
                command = await self._queue.get()
                try:
                    if isinstance(command, StartAttemptCommand):
                        attempt_id = await self._start_attempt(session, command)
                        await session.commit()
                        command.future.set_result(attempt_id)
                    elif isinstance(command, FinishAttemptCommand):
                        await self._finish_attempt(session, command.completion)
                        await session.commit()
                        command.future.set_result(None)
                    elif isinstance(command, FinalizeItemCommand):
                        await self._finalize_item(session, command)
                        await session.commit()
                        command.future.set_result(None)
                    elif isinstance(command, FinishInferenceCommand):
                        inference_complete = await self._finish_inference(session)
                        await session.commit()
                        command.future.set_result(inference_complete)
                    else:
                        command.future.set_result(None)
                        return
                except BaseException as error:
                    await session.rollback()
                    if not command.future.done():
                        command.future.set_exception(error)
                finally:
                    self._queue.task_done()

    async def _start_attempt(
        self, session: AsyncSession, command: StartAttemptCommand
    ) -> UUID | None:
        if await self._active_run(session) is None:
            return None
        item = await session.get(ExecutionItem, command.item_id)
        if item is None:
            raise RuntimeError("execution item disappeared")
        if (
            item.status
            not in {
                ExecutionStatus.PENDING,
                ExecutionStatus.IN_PROGRESS,
            }
            or item.attempt_count + 1 != command.attempt_no
        ):
            raise RuntimeError("execution item is not ready for the requested attempt")
        item.status = ExecutionStatus.IN_PROGRESS
        item.attempt_count = command.attempt_no
        attempt = HttpAttempt(
            execution_item_id=item.id,
            attempt_no=command.attempt_no,
            status=AttemptStatus.IN_PROGRESS,
        )
        session.add(attempt)
        await session.flush()
        return attempt.id

    async def _finish_attempt(self, session: AsyncSession, completion: AttemptCompletion) -> None:
        if await self._active_run(session) is None:
            return
        attempt = await session.get(HttpAttempt, completion.attempt_id)
        if attempt is None:
            raise RuntimeError("HTTP attempt disappeared")
        attempt.status = completion.status
        attempt.finished_at = utc_now()
        attempt.status_code = completion.status_code
        attempt.latency_ms = completion.latency_ms
        attempt.input_tokens = completion.input_tokens
        attempt.output_tokens = completion.output_tokens
        attempt.total_tokens = completion.total_tokens
        attempt.error_type = completion.error_type

    async def _finalize_item(self, session: AsyncSession, command: FinalizeItemCommand) -> None:
        if await self._active_run(session) is None:
            return
        item = await session.get(ExecutionItem, command.item_id)
        if item is None:
            raise RuntimeError("execution item disappeared")
        outcome = command.outcome
        if outcome.response_content is not None:
            stored = await asyncio.to_thread(
                store_agent_response,
                self._artifact_store,
                run_id=self._run_id,
                agent_id=command.agent_id,
                question_id=command.question_id,
                content=outcome.response_content,
            )
            artifact = ArtifactIndex(
                kind=ArtifactKind.AGENT_RESPONSE,
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                expires_at=utc_now() + timedelta(days=self._settings.artifact_retention_days),
            )
            session.add(artifact)
            await session.flush()
            item.artifact_id = artifact.id
        if command.trajectory is not None:
            stored = await asyncio.to_thread(
                store_trajectory,
                self._artifact_store,
                run_id=self._run_id,
                agent_id=command.agent_id,
                question_id=command.question_id,
                content=json.dumps(command.trajectory, ensure_ascii=False).encode(),
            )
            artifact = ArtifactIndex(
                kind=ArtifactKind.TRAJECTORY,
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                expires_at=utc_now() + timedelta(days=self._settings.artifact_retention_days),
            )
            session.add(artifact)
            await session.flush()
            item.artifact_id = artifact.id
        if command.task_reward is not None:
            item.task_reward = command.task_reward
        if command.simulator_input_tokens is not None:
            item.simulator_input_tokens = command.simulator_input_tokens
        if command.simulator_output_tokens is not None:
            item.simulator_output_tokens = command.simulator_output_tokens

        item.status = ExecutionStatus.ANSWERED if outcome.success else ExecutionStatus.FAILED
        item.latency_ms = outcome.latency_ms
        item.error_type = outcome.error_type
        if outcome.answer is not None:
            item.answer_preview = outcome.answer[:500]
            item.answer_hash = hashlib.sha256(outcome.answer.encode()).hexdigest()
            if outcome.success:
                if self._bad_pattern_rules is None:
                    self._bad_pattern_rules = list(
                        await session.scalars(
                            select(BadPatternRule)
                            .where(BadPatternRule.enabled.is_(True))
                            .order_by(BadPatternRule.key)
                        )
                    )
                await scan_bad_patterns(
                    session,
                    execution_item_id=item.id,
                    answer=outcome.answer,
                    rules=self._bad_pattern_rules,
                    refresh_existing=False,
                )
        requires_citations = await session.scalar(
            select(Question.requires_citations).where(Question.id == item.question_id)
        )
        if requires_citations is None:
            raise RuntimeError("execution item question disappeared")
        if outcome.success and requires_citations:
            session.add_all(
                [
                    CitationRecord(
                        execution_item_id=item.id,
                        position=position,
                        original_url=url,
                    )
                    for position, url in enumerate(outcome.citations[:3])
                ]
            )
        await session.flush()
        await self._refresh_run_agent_counts(session, command.run_agent_id)

    async def _refresh_run_agent_counts(self, session: AsyncSession, run_agent_id: UUID) -> None:
        run_agent = await session.get(RunAgent, run_agent_id)
        if run_agent is None:
            raise RuntimeError("run agent disappeared")
        rows = list(
            await session.execute(
                select(ExecutionItem.status, ExecutionItem.attempt_count).where(
                    ExecutionItem.run_agent_id == run_agent_id
                )
            )
        )
        run_agent.completed = sum(
            status in {ExecutionStatus.ANSWERED, ExecutionStatus.SCORED} for status, _ in rows
        )
        run_agent.failed = sum(status == ExecutionStatus.FAILED for status, _ in rows)
        run_agent.retried = sum(attempt_count >= 2 for _, attempt_count in rows)
        if run_agent.completed + run_agent.failed == run_agent.total:
            run_agent.status = RunAgentStatus.COMPLETED

    async def _finish_inference(self, session: AsyncSession) -> bool:
        run = await self._active_run(session)
        if run is None:
            return False
        unfinished = await session.scalar(
            select(func.count())
            .select_from(ExecutionItem)
            .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
            .where(
                RunAgent.run_id == self._run_id,
                ExecutionItem.status.in_([ExecutionStatus.PENDING, ExecutionStatus.IN_PROGRESS]),
            )
        )
        if unfinished:
            return False
        run.status = RunStatus.SCORING
        run.stage = RunStage.JUDGING
        run.heartbeat_at = utc_now()
        return True

    async def _active_run(self, session: AsyncSession) -> EvaluationRun | None:
        run = await lock_owned_run(session, self._run_id, self._owner_id)
        if (
            run is None
            or run.desired_state != DesiredRunState.RUNNING
            or run.status != RunStatus.RUNNING
            or run.active_slot is None
        ):
            return None
        return run
