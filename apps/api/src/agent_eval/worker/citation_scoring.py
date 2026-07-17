import asyncio
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload, selectinload

from agent_eval.citations.extraction import extract_citation_text
from agent_eval.core.artifact_store import ArtifactStore
from agent_eval.core.artifacts import read_indexed_artifact, store_citation_judge_response
from agent_eval.core.settings import Settings
from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    ArtifactKind,
    CitationFetchStatus,
    DesiredRunState,
    ExecutionStatus,
    RunStage,
    RunStatus,
)
from agent_eval.db.models import (
    ArtifactIndex,
    CitationRecord,
    EvaluationRun,
    ExecutionItem,
    JudgeConfig,
    Question,
    RunAgent,
    ScoreConfig,
)
from agent_eval.judges.deepseek import JudgeError, Sleep, judge_citation_support
from agent_eval.runs.queue import lock_owned_run
from agent_eval.scoring.citations import (
    calculate_citation_score,
    calculate_retrieval_final_score,
)
from agent_eval.worker.limits import RequestLimit, concurrency_slot
from agent_eval.worker.scoring import load_candidate_answer


@dataclass(frozen=True)
class CitationScoreWork:
    citation_id: UUID
    fetch_status: CitationFetchStatus
    fetch_reason: str | None
    question: str
    candidate_answer: str
    artifact_path: str | None
    artifact_sha256: str | None
    artifact_size: int | None


@dataclass(frozen=True)
class CitationScoreResult:
    citation_id: UUID
    authenticity_score: int
    support_score: int
    reason: str
    fetch_status: CitationFetchStatus | None = None
    raw_judge_response: bytes | None = None


@dataclass(frozen=True)
class WriteCitationScoreCommand:
    result: CitationScoreResult
    future: asyncio.Future[bool]


@dataclass(frozen=True)
class FinishCitationScoringCommand:
    future: asyncio.Future[bool]


@dataclass(frozen=True)
class StopCitationScoringWriter:
    future: asyncio.Future[None]


CitationScoringCommand = (
    WriteCitationScoreCommand | FinishCitationScoringCommand | StopCitationScoringWriter
)


class CitationScoringWriter:
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
        self._queue: asyncio.Queue[CitationScoringCommand] = asyncio.Queue()

    async def write(self, result: CitationScoreResult) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        await self._queue.put(WriteCitationScoreCommand(result, future))
        return await future

    async def finish(self) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        await self._queue.put(FinishCitationScoringCommand(future))
        return await future

    async def stop(self) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put(StopCitationScoringWriter(future))
        await future

    async def run(self) -> None:
        async with self._session_factory() as session:
            while True:
                command = await self._queue.get()
                try:
                    if isinstance(command, StopCitationScoringWriter):
                        command.future.set_result(None)
                        return
                    if isinstance(command, WriteCitationScoreCommand):
                        result = await self._write(session, command.result)
                    else:
                        result = await self._finish(session)
                    await session.commit()
                    command.future.set_result(result)
                except BaseException as error:
                    await session.rollback()
                    if not command.future.done():
                        command.future.set_exception(error)
                finally:
                    self._queue.task_done()

    async def _run_is_active(self, session: AsyncSession) -> bool:
        run = await lock_owned_run(session, self._run_id, self._owner_id)
        return (
            run is not None
            and run.desired_state == DesiredRunState.RUNNING
            and run.status == RunStatus.SCORING
            and run.stage == RunStage.JUDGING
        )

    async def _write(self, session: AsyncSession, result: CitationScoreResult) -> bool:
        if not await self._run_is_active(session):
            return False
        citation = await session.get(CitationRecord, result.citation_id)
        if citation is None:
            raise RuntimeError("citation disappeared during support scoring")
        if citation.support_score is not None:
            return True

        judge_artifact_id: UUID | None = None
        if result.raw_judge_response is not None:
            stored = await asyncio.to_thread(
                store_citation_judge_response,
                self._artifact_store,
                run_id=self._run_id,
                citation_id=citation.id,
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

        citation.authenticity_score = Decimal(result.authenticity_score)
        citation.support_score = Decimal(result.support_score)
        citation.judge_reason = result.reason[:500]
        citation.judge_artifact_id = judge_artifact_id
        if result.fetch_status is not None:
            citation.fetch_status = result.fetch_status
        return True

    async def _finish(self, session: AsyncSession) -> bool:
        if not await self._run_is_active(session):
            return False
        unfinished_id = await session.scalar(
            select(CitationRecord.id)
            .join(ExecutionItem, ExecutionItem.id == CitationRecord.execution_item_id)
            .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
            .join(Question, Question.id == ExecutionItem.question_id)
            .where(
                RunAgent.run_id == self._run_id,
                Question.requires_citations.is_(True),
                CitationRecord.support_score.is_(None),
            )
            .limit(1)
        )
        if unfinished_id is not None:
            return False

        run = await session.get(EvaluationRun, self._run_id)
        if run is None:
            raise RuntimeError("evaluation run disappeared after citation scoring")
        score_config = await session.get(ScoreConfig, run.score_config_id)
        if score_config is None:
            raise RuntimeError("score configuration is unavailable")
        items = list(
            await session.scalars(
                select(ExecutionItem)
                .options(joinedload(ExecutionItem.score), selectinload(ExecutionItem.citations))
                .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                .join(Question, Question.id == ExecutionItem.question_id)
                .where(RunAgent.run_id == self._run_id, Question.requires_citations.is_(True))
                .order_by(ExecutionItem.id)
            )
        )
        for item in items:
            score = item.score
            if score is None:
                raise RuntimeError("citation item has no answer score")
            citation_values: list[tuple[Decimal, Decimal]] = []
            for citation in item.citations:
                if citation.authenticity_score is None or citation.support_score is None:
                    raise RuntimeError("citation score disappeared during finalization")
                citation_values.append((citation.authenticity_score, citation.support_score))
            citation_score = calculate_citation_score(
                citation_values,
                authenticity_weight=score_config.citation_authenticity_weight,
                support_weight=score_config.citation_support_weight,
            )
            score.citation_score = citation_score
            score.final_score = calculate_retrieval_final_score(
                score.answer_score,
                citation_score,
                answer_weight=score_config.retrieval_answer_weight,
                citation_weight=score_config.retrieval_citation_weight,
            )
            if item.status == ExecutionStatus.ANSWERED:
                item.status = ExecutionStatus.SCORED
        run.stage = RunStage.AGGREGATING
        run.heartbeat_at = utc_now()
        return True


async def execute_citation_scoring(
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
    work, judge_model = await load_citation_score_work(
        session_factory, settings, artifact_store, run_id
    )
    if work is None:
        return True

    writer = CitationScoringWriter(session_factory, settings, run_id, owner_id, artifact_store)
    writer_task = asyncio.create_task(writer.run(), name=f"citation-scoring-writer-{run_id}")
    try:
        queue: asyncio.Queue[CitationScoreWork] = asyncio.Queue()
        for item in work:
            queue.put_nowait(item)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            workers = [
                asyncio.create_task(
                    citation_score_worker(
                        queue,
                        writer,
                        client,
                        settings,
                        artifact_store,
                        judge_model,
                        sleep,
                        request_limit,
                    ),
                    name=f"citation-judge-{index}",
                )
                for index in range(min(settings.deepseek_concurrency, len(work)))
            ]
            if workers and not all(await asyncio.gather(*workers)):
                return False
        return await writer.finish()
    finally:
        await writer.stop()
        await writer_task


async def load_citation_score_work(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    artifact_store: ArtifactStore,
    run_id: UUID,
) -> tuple[list[CitationScoreWork] | None, str]:
    async with session_factory() as session:
        run = await session.get(EvaluationRun, run_id)
        if run is None:
            raise RuntimeError("evaluation run disappeared before citation scoring")
        judge_config = await session.get(JudgeConfig, run.judge_config_id)
        if judge_config is None or not judge_config.enabled:
            raise RuntimeError("judge configuration is unavailable")
        if run.stage != RunStage.JUDGING:
            return None, judge_config.model
        items = list(
            await session.scalars(
                select(ExecutionItem)
                .options(
                    joinedload(ExecutionItem.question),
                    joinedload(ExecutionItem.artifact),
                    joinedload(ExecutionItem.run_agent).joinedload(RunAgent.agent_version),
                    selectinload(ExecutionItem.citations).joinedload(CitationRecord.artifact),
                )
                .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                .join(Question, Question.id == ExecutionItem.question_id)
                .where(RunAgent.run_id == run_id, Question.requires_citations.is_(True))
                .order_by(ExecutionItem.id)
            )
        )
        model = judge_config.model

    work: list[CitationScoreWork] = []
    for item in items:
        if item.status == ExecutionStatus.FAILED:
            candidate_answer = ""
        else:
            candidate_answer = await load_candidate_answer(artifact_store, item)
        for citation in sorted(item.citations, key=lambda value: value.position):
            if citation.support_score is not None:
                continue
            artifact = citation.artifact
            if citation.fetch_status == CitationFetchStatus.FETCHED and artifact is None:
                raise RuntimeError("fetched citation has no content artifact")
            work.append(
                CitationScoreWork(
                    citation_id=citation.id,
                    fetch_status=citation.fetch_status,
                    fetch_reason=citation.judge_reason,
                    question=item.question.prompt,
                    candidate_answer=candidate_answer,
                    artifact_path=artifact.relative_path if artifact is not None else None,
                    artifact_sha256=artifact.sha256 if artifact is not None else None,
                    artifact_size=artifact.size_bytes if artifact is not None else None,
                )
            )
    return work, model


async def citation_score_worker(
    queue: asyncio.Queue[CitationScoreWork],
    writer: CitationScoringWriter,
    client: httpx.AsyncClient,
    settings: Settings,
    artifact_store: ArtifactStore,
    model: str,
    sleep: Sleep,
    request_limit: RequestLimit | None = None,
) -> bool:
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            return True
        try:
            if item.fetch_status != CitationFetchStatus.FETCHED:
                result = CitationScoreResult(
                    citation_id=item.citation_id,
                    authenticity_score=0,
                    support_score=0,
                    reason=item.fetch_reason or item.fetch_status.value,
                )
            else:
                source_text = await load_source_text(settings, artifact_store, item)
                if source_text is None:
                    result = CitationScoreResult(
                        citation_id=item.citation_id,
                        authenticity_score=0,
                        support_score=0,
                        reason="content_extraction_failed",
                        fetch_status=CitationFetchStatus.UNSUPPORTED,
                    )
                else:
                    api_key = settings.deepseek_api_key
                    if api_key is None:
                        raise RuntimeError("DeepSeek API Key 未配置")
                    try:
                        async with concurrency_slot(request_limit):
                            judged = await judge_citation_support(
                                client,
                                api_url=settings.deepseek_api_url,
                                api_key=api_key.get_secret_value(),
                                model=model,
                                question=item.question,
                                candidate_answer=item.candidate_answer,
                                source_text=source_text,
                                timeout_seconds=settings.deepseek_timeout_seconds,
                                sleep=sleep,
                            )
                    except JudgeError as error:
                        if error.error_type != "judge_invalid_response":
                            raise
                        result = CitationScoreResult(
                            citation_id=item.citation_id,
                            authenticity_score=100,
                            support_score=0,
                            reason="judge_invalid_response_after_retry",
                        )
                    else:
                        result = CitationScoreResult(
                            citation_id=item.citation_id,
                            authenticity_score=100,
                            support_score=judged.score,
                            reason=judged.reason,
                            raw_judge_response=judged.raw_response,
                        )
            if not await writer.write(result):
                return False
        finally:
            queue.task_done()


async def load_source_text(
    settings: Settings, artifact_store: ArtifactStore, item: CitationScoreWork
) -> str | None:
    if (
        item.artifact_path is None
        or item.artifact_sha256 is None
        or item.artifact_size is None
    ):
        raise RuntimeError("fetched citation artifact metadata is incomplete")
    content = await asyncio.to_thread(
        read_indexed_artifact,
        artifact_store,
        relative_path=item.artifact_path,
        expected_sha256=item.artifact_sha256,
        expected_size=item.artifact_size,
    )
    return await asyncio.to_thread(
        extract_citation_text,
        content,
        max_bytes=settings.citation_max_text_bytes,
    )
