import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload, selectinload

from agent_eval.agents.configuration import inference_config_for_version
from agent_eval.agents.inference import (
    InferenceConfig,
    InferenceOutcome,
    Sleep,
    execute_inference,
)
from agent_eval.agents.prompt import build_question_prompt
from agent_eval.auth.encryption import TokenCipher
from agent_eval.core.artifact_store import ArtifactStore
from agent_eval.core.settings import Settings
from agent_eval.db.enums import ExecutionStatus, QuestionType
from agent_eval.db.models import AgentVersion, DatasetVersion, ExecutionItem, Question, RunAgent
from agent_eval.worker.limits import RequestLimit, agent_request_slots
from agent_eval.worker.result_writer import ResultWriter

TransportFactory = Callable[[UUID], httpx.AsyncBaseTransport | None]


@dataclass(frozen=True)
class WorkItem:
    id: UUID
    run_agent_id: UUID
    question_id: UUID
    attempt_count: int
    prompt: str
    question_type: QuestionType
    reference_answer: str
    trial_index: int


@dataclass(frozen=True)
class AgentWork:
    run_agent_id: UUID
    agent_id: UUID
    version_id: UUID
    config: InferenceConfig
    concurrency: int
    items: list[WorkItem]


class AgentCircuitBreaker:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._fatal_error: str | None = None
        self._input_limit_characters: int | None = None
        self._last_deterministic_error: str | None = None
        self._consecutive_deterministic_errors = 0

    async def skip_reason(self, item: WorkItem) -> str | None:
        async with self._lock:
            if self._fatal_error is not None:
                return f"agent_circuit_{self._fatal_error.removeprefix('agent_')}"
            if (
                self._input_limit_characters is not None
                and len(item.prompt) >= self._input_limit_characters
            ):
                return "agent_circuit_input_limit"
            return None

    async def record(self, outcome: InferenceOutcome, prompt_characters: int) -> None:
        if outcome.canceled:
            return
        async with self._lock:
            if outcome.success:
                self._last_deterministic_error = None
                self._consecutive_deterministic_errors = 0
                return
            if outcome.error_type == "agent_authentication":
                self._fatal_error = outcome.error_type
                return
            if outcome.error_type == "agent_input_limit":
                self._input_limit_characters = min(
                    self._input_limit_characters or prompt_characters,
                    prompt_characters,
                )
                return
            if outcome.error_type not in {"agent_client_error", "agent_response_schema"}:
                self._last_deterministic_error = None
                self._consecutive_deterministic_errors = 0
                return
            if outcome.error_type == self._last_deterministic_error:
                self._consecutive_deterministic_errors += 1
            else:
                self._last_deterministic_error = outcome.error_type
                self._consecutive_deterministic_errors = 1
            if self._consecutive_deterministic_errors >= 2:
                self._fatal_error = outcome.error_type


async def execute_run_inference(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    run_id: UUID,
    *,
    artifact_store: ArtifactStore,
    token_cipher: TokenCipher | None,
    owner_id: str,
    transport_factory: TransportFactory | None = None,
    sleep: Sleep = asyncio.sleep,
    request_limit: RequestLimit | None = None,
    version_request_limit: RequestLimit | None = None,
    simulator_request_limit: RequestLimit | None = None,
) -> bool:
    work = await load_run_work(session_factory, run_id, token_cipher)
    writer = ResultWriter(session_factory, settings, run_id, owner_id, artifact_store)
    writer_task = asyncio.create_task(writer.run(), name=f"result-writer-{run_id}")
    try:
        await asyncio.gather(
            *[
                execute_agent_work(
                    agent_work,
                    writer,
                    settings=settings,
                    transport=(
                        transport_factory(agent_work.version_id)
                        if transport_factory is not None
                        else None
                    ),
                    sleep=sleep,
                    request_limit=request_limit,
                    version_request_limit=version_request_limit,
                    simulator_request_limit=simulator_request_limit,
                )
                for agent_work in work
            ]
        )
        return await writer.finish_inference()
    finally:
        await writer.stop()
        await writer_task


async def load_run_work(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    token_cipher: TokenCipher | None,
) -> list[AgentWork]:
    async with session_factory() as session:
        run_agents = list(
            await session.scalars(
                select(RunAgent)
                .options(
                    joinedload(RunAgent.agent_version).joinedload(AgentVersion.agent),
                    selectinload(RunAgent.execution_items)
                    .joinedload(ExecutionItem.question)
                    .joinedload(Question.dataset_version)
                    .joinedload(DatasetVersion.benchmark),
                )
                .where(RunAgent.run_id == run_id)
                .order_by(RunAgent.id)
            )
        )

    work: list[AgentWork] = []
    for run_agent in run_agents:
        version = run_agent.agent_version
        if version.agent.owner_user_id is not None:
            raise RuntimeError("formal evaluation run contains a private Agent")
        items = [
            WorkItem(
                id=item.id,
                run_agent_id=run_agent.id,
                question_id=item.question_id,
                attempt_count=item.attempt_count,
                prompt=(
                    item.question.prompt
                    if item.question.type == QuestionType.INTERACTIVE
                    else question_prompt(item.question)
                ),
                question_type=item.question.type,
                reference_answer=item.question.reference_answer,
                trial_index=item.trial_index,
            )
            for item in sorted(run_agent.execution_items, key=lambda value: str(value.question_id))
            if item.status == ExecutionStatus.PENDING
        ]
        work.append(
            AgentWork(
                run_agent_id=run_agent.id,
                agent_id=version.agent_id,
                version_id=version.id,
                config=inference_config_for_version(version, token_cipher, None),
                concurrency=version.concurrency,
                items=items,
            )
        )
    return work


def question_prompt(question: Question) -> str:
    return build_question_prompt(
        question_type=question.type,
        prompt=question.prompt,
        choices=question.choices_json,
        requires_citations=question.requires_citations,
        ability=question.dataset_version.benchmark.ability,
    )


async def execute_agent_work(
    work: AgentWork,
    writer: ResultWriter,
    *,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None,
    sleep: Sleep,
    request_limit: RequestLimit | None = None,
    version_request_limit: RequestLimit | None = None,
    simulator_request_limit: RequestLimit | None = None,
) -> None:
    circuit_breaker = AgentCircuitBreaker()
    interactive_items = [
        item for item in work.items if item.question_type == QuestionType.INTERACTIVE
    ]
    standard_items = [
        item for item in work.items if item.question_type != QuestionType.INTERACTIVE
    ]
    phases: list[Coroutine[Any, Any, None]] = []
    if standard_items:
        phases.append(
            _execute_standard_items(
                standard_items,
                work,
                writer,
                circuit_breaker,
                transport=transport,
                sleep=sleep,
                request_limit=request_limit,
                version_request_limit=version_request_limit,
            )
        )
    if interactive_items:
        from agent_eval.worker.interactive_executor import (
            InteractiveWorkItem,
            execute_interactive_items,
        )

        phases.append(
            execute_interactive_items(
                [
                    InteractiveWorkItem(
                        item_id=item.id,
                        run_agent_id=item.run_agent_id,
                        question_id=item.question_id,
                        attempt_count=item.attempt_count,
                        trial_index=item.trial_index,
                        task_ref=item.reference_answer,
                        prompt_characters=len(item.prompt),
                    )
                    for item in interactive_items
                ],
                agent_id=work.agent_id,
                version_id=work.version_id,
                concurrency=work.concurrency,
                config=work.config,
                settings=settings,
                writer=writer,
                record_outcome=circuit_breaker.record,
                request_limit=request_limit,
                version_request_limit=version_request_limit,
                simulator_request_limit=simulator_request_limit,
            )
        )
    if phases:
        await asyncio.gather(*phases)


async def _execute_standard_items(
    items: list[WorkItem],
    work: AgentWork,
    writer: ResultWriter,
    circuit_breaker: AgentCircuitBreaker,
    *,
    transport: httpx.AsyncBaseTransport | None,
    sleep: Sleep,
    request_limit: RequestLimit | None = None,
    version_request_limit: RequestLimit | None = None,
) -> None:
    item_queue: asyncio.Queue[WorkItem] = asyncio.Queue()
    for item in items:
        item_queue.put_nowait(item)

    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        workers = [
            asyncio.create_task(
                agent_worker(
                    item_queue,
                    work,
                    writer,
                    client,
                    sleep,
                    circuit_breaker,
                    request_limit,
                    version_request_limit,
                ),
                name=f"agent-{work.version_id}-{index}",
            )
            for index in range(min(work.concurrency, len(items)))
        ]
        if workers:
            await asyncio.gather(*workers)


async def agent_worker(
    item_queue: asyncio.Queue[WorkItem],
    work: AgentWork,
    writer: ResultWriter,
    client: httpx.AsyncClient,
    sleep: Sleep,
    circuit_breaker: AgentCircuitBreaker,
    request_limit: RequestLimit | None = None,
    version_request_limit: RequestLimit | None = None,
) -> None:
    while True:
        try:
            item = item_queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            skip_reason = await circuit_breaker.skip_reason(item)
            if skip_reason is not None:
                await writer.finalize_item(
                    item_id=item.id,
                    run_agent_id=item.run_agent_id,
                    agent_id=work.agent_id,
                    question_id=item.question_id,
                    outcome=InferenceOutcome(
                        success=False,
                        canceled=False,
                        answer=None,
                        citations=[],
                        response_content=None,
                        latency_ms=0,
                        attempt_count=item.attempt_count,
                        error_type=skip_reason,
                    ),
                )
                continue
            async with agent_request_slots(
                version_request_limit,
                request_limit,
                version_id=str(work.version_id),
                version_limit=work.concurrency,
            ):
                outcome = await execute_inference(
                    client,
                    work.config,
                    item.prompt,
                    previous_attempt_count=item.attempt_count,
                    start_attempt=partial(writer.start_attempt, item.id),
                    finish_attempt=writer.finish_attempt,
                    sleep=sleep,
                )
            if outcome.canceled:
                return
            await writer.finalize_item(
                item_id=item.id,
                run_agent_id=item.run_agent_id,
                agent_id=work.agent_id,
                question_id=item.question_id,
                outcome=outcome,
            )
            await circuit_breaker.record(outcome, len(item.prompt))
        finally:
            item_queue.task_done()
