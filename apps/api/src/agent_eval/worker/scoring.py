import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from agent_eval.agents.mapping import extract_answer
from agent_eval.core.artifact_store import ArtifactStore
from agent_eval.core.artifacts import read_indexed_artifact, store_judge_response
from agent_eval.core.settings import Settings
from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    Ability,
    ArtifactKind,
    DesiredRunState,
    ExecutionStatus,
    QuestionType,
    RunStage,
    RunStatus,
)
from agent_eval.db.models import (
    AgentVersion,
    ArtifactIndex,
    DatasetVersion,
    EvaluationRun,
    ExecutionItem,
    JudgeConfig,
    Question,
    RunAgent,
    ScoreRecord,
)
from agent_eval.judges.deepseek import (
    JUDGE_TEMPERATURE,
    RUBRIC_PROMPT_VERSION,
    JudgeError,
    Sleep,
    judge_open_answer,
    judge_rubric_answer,
    judge_short_answer,
)
from agent_eval.runs.queue import lock_owned_run
from agent_eval.scoring.rules import score_exact_answer, score_multiple_choice
from agent_eval.worker.limits import RequestLimit, concurrency_slot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JudgeRuntime:
    provider: str
    model: str
    prompt_version: str


@dataclass(frozen=True)
class ScoreWork:
    item_id: UUID
    ability: Ability
    question_type: QuestionType
    question: str
    reference_answer: str
    candidate_answer: str | None
    valid_labels: list[str]
    requires_citations: bool
    failed: bool
    task_reward: Decimal | None = None
    reward_summary: str = ""
    simulator_input_tokens: int | None = None
    simulator_output_tokens: int | None = None


@dataclass(frozen=True)
class ScoreResult:
    item_id: UUID
    answer_score: int
    reason: str
    requires_citations: bool
    failed: bool
    raw_judge_response: bytes | None = None
    judge_provider: str | None = None
    judge_model_requested: str | None = None
    judge_model_returned: str | None = None
    judge_prompt_version: str | None = None
    judge_temperature: float | None = None
    judge_details: dict[str, Any] | None = None


@dataclass(frozen=True)
class WriteScoreCommand:
    result: ScoreResult
    future: asyncio.Future[bool]


@dataclass(frozen=True)
class StopScoringWriter:
    future: asyncio.Future[None]


ScoringCommand = WriteScoreCommand | StopScoringWriter


class AnswerScoringError(RuntimeError):
    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


class ScoringWriter:
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
        self._queue: asyncio.Queue[ScoringCommand] = asyncio.Queue()

    async def write(self, result: ScoreResult) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        await self._queue.put(WriteScoreCommand(result, future))
        return await future

    async def stop(self) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put(StopScoringWriter(future))
        await future

    async def run(self) -> None:
        async with self._session_factory() as session:
            while True:
                command = await self._queue.get()
                try:
                    if isinstance(command, StopScoringWriter):
                        command.future.set_result(None)
                        return
                    written = await self._write_score(session, command.result)
                    await session.commit()
                    command.future.set_result(written)
                except BaseException as error:
                    await session.rollback()
                    if not command.future.done():
                        command.future.set_exception(error)
                finally:
                    self._queue.task_done()

    async def _write_score(self, session: AsyncSession, result: ScoreResult) -> bool:
        run = await lock_owned_run(session, self._run_id, self._owner_id)
        if (
            run is None
            or run.desired_state != DesiredRunState.RUNNING
            or run.status != RunStatus.SCORING
            or run.stage != RunStage.JUDGING
        ):
            return False
        existing = await session.scalar(
            select(ScoreRecord).where(ScoreRecord.execution_item_id == result.item_id)
        )
        if existing is not None:
            return True

        score_id = uuid4()
        judge_artifact_id: UUID | None = None
        if result.raw_judge_response is not None:
            stored = await asyncio.to_thread(
                store_judge_response,
                self._artifact_store,
                run_id=self._run_id,
                score_id=score_id,
                content=result.raw_judge_response,
            )
            artifact = ArtifactIndex(
                kind=ArtifactKind.JUDGE_RESPONSE,
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                expires_at=utc_now() + timedelta(days=self._settings.artifact_retention_days),
            )
            session.add(artifact)
            await session.flush()
            judge_artifact_id = artifact.id

        score = ScoreRecord(
            id=score_id,
            execution_item_id=result.item_id,
            answer_score=result.answer_score,
            citation_score=None,
            final_score=result.answer_score,
            judge_reason=result.reason[:500],
            judge_artifact_id=judge_artifact_id,
            judge_provider=result.judge_provider,
            judge_model_requested=result.judge_model_requested,
            judge_model_returned=result.judge_model_returned,
            judge_prompt_version=result.judge_prompt_version,
            judge_temperature=(
                Decimal(str(result.judge_temperature))
                if result.judge_temperature is not None
                else None
            ),
            judge_details_json=result.judge_details,
        )
        session.add(score)
        item = await session.get(ExecutionItem, result.item_id)
        if item is None:
            raise RuntimeError("execution item disappeared during scoring")
        if not result.failed and not result.requires_citations:
            item.status = ExecutionStatus.SCORED
        return True


async def execute_answer_scoring(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    run_id: UUID,
    *,
    artifact_store: ArtifactStore,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
    owner_id: str,
    request_limit: RequestLimit | None = None,
) -> bool:
    work, judge_runtime = await load_score_work(session_factory, settings, artifact_store, run_id)
    writer = ScoringWriter(session_factory, settings, run_id, owner_id, artifact_store)
    writer_task = asyncio.create_task(writer.run(), name=f"scoring-writer-{run_id}")
    try:
        local_work = [
            item
            for item in work
            if item.failed
            or item.question_type == QuestionType.MULTIPLE_CHOICE
            or item.question_type == QuestionType.INTERACTIVE
            or item.ability == Ability.INSTRUCTION_FOLLOWING
        ]
        for item in local_work:
            result = local_score(item)
            if not await writer.write(result):
                return False

        judge_work = [
            item
            for item in work
            if not item.failed
            and item.question_type in {QuestionType.SHORT_ANSWER, QuestionType.OPEN_ENDED}
            and item.ability != Ability.INSTRUCTION_FOLLOWING
        ]
        if judge_work:
            if settings.deepseek_api_key is None:
                raise RuntimeError("DeepSeek API Key 未配置")
            queue: asyncio.Queue[ScoreWork] = asyncio.Queue()
            for item in judge_work:
                queue.put_nowait(item)
            async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
                workers = [
                    asyncio.create_task(
                        judge_worker(
                            queue,
                            writer,
                            client,
                            settings,
                            judge_runtime,
                            sleep,
                            request_limit,
                        ),
                        name=f"answer-judge-{index}",
                    )
                    for index in range(min(settings.deepseek_concurrency, len(judge_work)))
                ]
                judge_results = await asyncio.gather(*workers, return_exceptions=True)
                failures = [result for result in judge_results if isinstance(result, BaseException)]
                if failures:
                    failure = failures[0]
                    error_type = (
                        failure.error_type
                        if isinstance(failure, JudgeError)
                        else f"judge_worker_{type(failure).__name__}"
                    )
                    raise AnswerScoringError(error_type)
                if not all(judge_results):
                    return False
        return await advance_after_answer_scoring(session_factory, run_id, owner_id=owner_id)
    finally:
        await writer.stop()
        await writer_task


async def load_score_work(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    artifact_store: ArtifactStore,
    run_id: UUID,
) -> tuple[list[ScoreWork], JudgeRuntime]:
    async with session_factory() as session:
        run = await session.get(EvaluationRun, run_id)
        if run is None:
            raise RuntimeError("evaluation run disappeared before scoring")
        judge_config = await session.get(JudgeConfig, run.judge_config_id)
        if judge_config is None or not judge_config.enabled:
            raise RuntimeError("judge configuration is unavailable")
        items = list(
            await session.scalars(
                select(ExecutionItem)
                .options(
                    joinedload(ExecutionItem.question)
                    .joinedload(Question.dataset_version)
                    .joinedload(DatasetVersion.benchmark),
                    joinedload(ExecutionItem.artifact),
                    joinedload(ExecutionItem.run_agent).joinedload(RunAgent.agent_version),
                    joinedload(ExecutionItem.score),
                )
                .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                .where(
                    RunAgent.run_id == run_id,
                    ExecutionItem.status.in_([ExecutionStatus.ANSWERED, ExecutionStatus.FAILED]),
                )
                .order_by(ExecutionItem.id)
            )
        )
        judge_runtime = JudgeRuntime(
            provider=judge_config.provider,
            model=judge_config.model,
            prompt_version=judge_config.prompt_version,
        )
    logger.info(
        "judge_config_resolved",
        extra={
            "run_id": str(run_id),
            "judge_provider": judge_runtime.provider,
            "judge_model": judge_runtime.model,
            "judge_prompt_version": judge_runtime.prompt_version,
        },
    )

    work: list[ScoreWork] = []
    for item in items:
        if item.score is not None:
            continue
        failed = item.status == ExecutionStatus.FAILED
        interactive = item.question.type == QuestionType.INTERACTIVE
        candidate = (
            None
            if failed or interactive
            else await load_candidate_answer(artifact_store, item)
        )
        choices = item.question.choices_json or []
        labels = [str(choice["label"]) for choice in choices if "label" in choice]
        work.append(
            ScoreWork(
                item_id=item.id,
                ability=item.question.dataset_version.benchmark.ability,
                question_type=item.question.type,
                question=item.question.prompt,
                reference_answer=item.question.reference_answer,
                candidate_answer=candidate,
                valid_labels=labels,
                requires_citations=item.question.requires_citations,
                failed=failed,
                task_reward=item.task_reward if interactive else None,
                reward_summary=(item.answer_preview or "") if interactive else "",
                simulator_input_tokens=item.simulator_input_tokens if interactive else None,
                simulator_output_tokens=item.simulator_output_tokens if interactive else None,
            )
        )
    return work, judge_runtime


async def load_candidate_answer(artifact_store: ArtifactStore, item: ExecutionItem) -> str:
    artifact = item.artifact
    if artifact is None:
        raise RuntimeError("answered execution item has no response artifact")
    content = await asyncio.to_thread(
        read_indexed_artifact,
        artifact_store,
        relative_path=artifact.relative_path,
        expected_sha256=artifact.sha256,
        expected_size=artifact.size_bytes,
    )
    payload = json.loads(content)
    version: AgentVersion = item.run_agent.agent_version
    return extract_answer(payload, version.answer_jsonpath)


def local_score(item: ScoreWork) -> ScoreResult:
    if item.failed:
        return ScoreResult(
            item_id=item.item_id,
            answer_score=0,
            reason="Agent 调用失败，答案记 0 分",
            requires_citations=item.requires_citations,
            failed=True,
        )
    if item.question_type == QuestionType.INTERACTIVE:
        return interactive_score(item)
    if item.candidate_answer is None:
        raise RuntimeError("rule scoring candidate answer is missing")
    scored = (
        score_exact_answer(item.candidate_answer, item.reference_answer)
        if item.ability == Ability.INSTRUCTION_FOLLOWING
        else score_multiple_choice(
            item.candidate_answer, item.reference_answer, item.valid_labels
        )
    )
    return ScoreResult(
        item_id=item.item_id,
        answer_score=scored.score,
        reason=scored.reason,
        requires_citations=item.requires_citations,
        failed=False,
    )


def interactive_score(item: ScoreWork) -> ScoreResult:
    """Score a tau2 interactive item from its stored task reward (no judge/rules)."""
    from agent_eval.worker.interactive_executor import (
        SIMULATOR_MODEL,
        TASK_SPLIT,
        parse_task_ref,
        tau2_version,
    )

    reward = item.task_reward if item.task_reward is not None else Decimal(0)
    try:
        domain, _task_id = parse_task_ref(item.reference_answer)
    except Exception:
        domain = "unknown"
    return ScoreResult(
        item_id=item.item_id,
        answer_score=round(float(reward) * 100),
        reason=item.reward_summary or "tau2 episode completed",
        requires_citations=item.requires_citations,
        failed=False,
        judge_provider="tau2",
        judge_model_requested=SIMULATOR_MODEL,
        judge_model_returned=SIMULATOR_MODEL,
        judge_prompt_version=f"tau2-{tau2_version()}",
        judge_temperature=0.0,
        judge_details={
            "tau2": {
                "tau2_version": tau2_version(),
                "domain": domain,
                "task_split": TASK_SPLIT,
                "task_reward": float(reward),
                "simulator_input_tokens": item.simulator_input_tokens,
                "simulator_output_tokens": item.simulator_output_tokens,
            }
        },
    )


async def judge_worker(
    queue: asyncio.Queue[ScoreWork],
    writer: ScoringWriter,
    client: httpx.AsyncClient,
    settings: Settings,
    judge_runtime: JudgeRuntime,
    sleep: Sleep,
    request_limit: RequestLimit | None = None,
) -> bool:
    api_key = settings.deepseek_api_key
    if api_key is None:
        raise RuntimeError("DeepSeek API Key 未配置")
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            return True
        try:
            if item.candidate_answer is None:
                raise RuntimeError("judge candidate answer is missing")
            score: int
            reason: str
            raw_response: bytes
            model_returned: str | None
            judge_details: dict[str, Any] | None = None
            async with concurrency_slot(request_limit):
                if (
                    item.question_type == QuestionType.OPEN_ENDED
                    and judge_runtime.prompt_version == RUBRIC_PROMPT_VERSION
                ):
                    judged_rubric = await judge_rubric_answer(
                        client,
                        api_url=settings.deepseek_api_url,
                        api_key=api_key.get_secret_value(),
                        model=judge_runtime.model,
                        question=item.question,
                        reference_answer=item.reference_answer,
                        candidate_answer=item.candidate_answer,
                        timeout_seconds=settings.deepseek_timeout_seconds,
                        sleep=sleep,
                    )
                    score = judged_rubric.score
                    reason = judged_rubric.reason
                    raw_response = judged_rubric.raw_response
                    model_returned = judged_rubric.model_returned
                    judge_details = {
                        "rubric": {
                            "prompt_version": RUBRIC_PROMPT_VERSION,
                            "dimensions": [
                                {
                                    "key": dimension.key,
                                    "label": dimension.label,
                                    "weight": dimension.weight,
                                    "verdict": dimension.verdict,
                                    "rationale": dimension.rationale,
                                }
                                for dimension in judged_rubric.dimensions
                            ],
                        }
                    }
                else:
                    judge = (
                        judge_open_answer
                        if item.question_type == QuestionType.OPEN_ENDED
                        else judge_short_answer
                    )
                    judged = await judge(
                        client,
                        api_url=settings.deepseek_api_url,
                        api_key=api_key.get_secret_value(),
                        model=judge_runtime.model,
                        question=item.question,
                        reference_answer=item.reference_answer,
                        candidate_answer=item.candidate_answer,
                        timeout_seconds=settings.deepseek_timeout_seconds,
                        sleep=sleep,
                    )
                    score = judged.score
                    reason = judged.reason
                    raw_response = judged.raw_response
                    model_returned = judged.model_returned
            written = await writer.write(
                ScoreResult(
                    item_id=item.item_id,
                    answer_score=score,
                    reason=reason,
                    requires_citations=item.requires_citations,
                    failed=False,
                    raw_judge_response=raw_response,
                    judge_provider=judge_runtime.provider,
                    judge_model_requested=judge_runtime.model,
                    judge_model_returned=model_returned,
                    judge_prompt_version=judge_runtime.prompt_version,
                    judge_temperature=JUDGE_TEMPERATURE,
                    judge_details=judge_details,
                )
            )
            if not written:
                return False
        finally:
            queue.task_done()


async def advance_after_answer_scoring(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    *,
    owner_id: str,
) -> bool:
    async with session_factory() as session:
        run = await lock_owned_run(session, run_id, owner_id)
        if run is None:
            return False
        if (
            run.desired_state != DesiredRunState.RUNNING
            or run.status != RunStatus.SCORING
            or run.stage != RunStage.JUDGING
        ):
            return False

        unscored_item_id = await session.scalar(
            select(ExecutionItem.id)
            .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
            .outerjoin(ScoreRecord, ScoreRecord.execution_item_id == ExecutionItem.id)
            .where(
                RunAgent.run_id == run_id,
                ExecutionItem.status.in_([ExecutionStatus.ANSWERED, ExecutionStatus.FAILED]),
                ScoreRecord.id.is_(None),
            )
            .limit(1)
        )
        if unscored_item_id is not None:
            return False

        citation_item_id = await session.scalar(
            select(ExecutionItem.id)
            .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
            .join(Question, Question.id == ExecutionItem.question_id)
            .where(
                RunAgent.run_id == run_id,
                ExecutionItem.status == ExecutionStatus.ANSWERED,
                Question.requires_citations.is_(True),
            )
            .limit(1)
        )
        run.stage = (
            RunStage.CITATION_FETCH if citation_item_id is not None else RunStage.AGGREGATING
        )
        run.heartbeat_at = utc_now()
        await session.commit()
        return True
