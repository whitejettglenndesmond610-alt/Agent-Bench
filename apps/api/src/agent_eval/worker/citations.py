import asyncio
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_eval.citations.fetcher import (
    BlockedUrlError,
    CitationFetchResult,
    Resolver,
    fetch_citation,
    parse_citation_url,
    resolve_addresses,
)
from agent_eval.core.artifact_store import ArtifactStore
from agent_eval.core.artifacts import store_citation_content
from agent_eval.core.settings import Settings
from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    ArtifactKind,
    CitationFetchStatus,
    DesiredRunState,
    RunStage,
    RunStatus,
)
from agent_eval.db.models import (
    ArtifactIndex,
    CitationRecord,
    EvaluationRun,
    ExecutionItem,
    Question,
    RunAgent,
)
from agent_eval.runs.queue import lock_owned_run
from agent_eval.worker.limits import RequestLimit, ResourceLimiter, concurrency_slot


@dataclass(frozen=True)
class CitationWork:
    id: UUID
    url: str
    domain: str


@dataclass(frozen=True)
class StartCitationCommand:
    citation_id: UUID
    future: asyncio.Future[bool]


@dataclass(frozen=True)
class FinishCitationCommand:
    citation_id: UUID
    result: CitationFetchResult
    future: asyncio.Future[bool]


@dataclass(frozen=True)
class FinishCitationStageCommand:
    future: asyncio.Future[bool]


@dataclass(frozen=True)
class StopCitationWriter:
    future: asyncio.Future[None]


CitationCommand = (
    StartCitationCommand
    | FinishCitationCommand
    | FinishCitationStageCommand
    | StopCitationWriter
)


class CitationWriter:
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
        self._queue: asyncio.Queue[CitationCommand] = asyncio.Queue()

    async def start(self, citation_id: UUID) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        await self._queue.put(StartCitationCommand(citation_id, future))
        return await future

    async def finish(self, citation_id: UUID, result: CitationFetchResult) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        await self._queue.put(FinishCitationCommand(citation_id, result, future))
        return await future

    async def finish_stage(self) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        await self._queue.put(FinishCitationStageCommand(future))
        return await future

    async def stop(self) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put(StopCitationWriter(future))
        await future

    async def run(self) -> None:
        async with self._session_factory() as session:
            while True:
                command = await self._queue.get()
                try:
                    if isinstance(command, StopCitationWriter):
                        command.future.set_result(None)
                        return
                    if isinstance(command, StartCitationCommand):
                        result = await self._start(session, command.citation_id)
                    elif isinstance(command, FinishCitationCommand):
                        result = await self._finish(session, command.citation_id, command.result)
                    else:
                        result = await self._finish_stage(session)
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
            and run.stage == RunStage.CITATION_FETCH
        )

    async def _start(self, session: AsyncSession, citation_id: UUID) -> bool:
        if not await self._run_is_active(session):
            return False
        citation = await session.get(CitationRecord, citation_id)
        if citation is None:
            raise RuntimeError("citation disappeared before fetch")
        if citation.fetch_status not in {
            CitationFetchStatus.PENDING,
            CitationFetchStatus.FETCHING,
        }:
            return True
        citation.fetch_status = CitationFetchStatus.FETCHING
        return True

    async def _finish(
        self,
        session: AsyncSession,
        citation_id: UUID,
        result: CitationFetchResult,
    ) -> bool:
        if not await self._run_is_active(session):
            return False
        citation = await session.get(CitationRecord, citation_id)
        if citation is None:
            raise RuntimeError("citation disappeared after fetch")
        if citation.fetch_status not in {
            CitationFetchStatus.PENDING,
            CitationFetchStatus.FETCHING,
        }:
            return True

        if result.content is not None:
            stored = await asyncio.to_thread(
                store_citation_content,
                self._artifact_store,
                run_id=self._run_id,
                citation_id=citation.id,
                content=result.content,
            )
            artifact = ArtifactIndex(
                kind=ArtifactKind.CITATION_CONTENT,
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                expires_at=utc_now() + timedelta(days=self._settings.artifact_retention_days),
            )
            session.add(artifact)
            await session.flush()
            citation.artifact_id = artifact.id

        citation.final_url = result.final_url
        citation.fetch_status = result.status
        citation.http_status = result.http_status
        citation.judge_reason = result.reason[:500]
        return True

    async def _finish_stage(self, session: AsyncSession) -> bool:
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
                CitationRecord.fetch_status.in_(
                    [CitationFetchStatus.PENDING, CitationFetchStatus.FETCHING]
                ),
            )
            .limit(1)
        )
        if unfinished_id is not None:
            return False
        run = await session.get(EvaluationRun, self._run_id)
        if run is None:
            raise RuntimeError("evaluation run disappeared after citation fetching")
        run.stage = RunStage.JUDGING
        run.heartbeat_at = utc_now()
        return True


async def execute_citation_fetch(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    run_id: UUID,
    *,
    artifact_store: ArtifactStore,
    resolver: Resolver = resolve_addresses,
    transport: httpx.AsyncBaseTransport | None = None,
    owner_id: str,
    request_limit: RequestLimit | None = None,
    domain_limit: ResourceLimiter | None = None,
) -> bool:
    work = await load_citation_work(session_factory, run_id)
    if work is None:
        return True

    writer = CitationWriter(session_factory, settings, run_id, owner_id, artifact_store)
    writer_task = asyncio.create_task(writer.run(), name=f"citation-writer-{run_id}")
    try:
        queue: asyncio.Queue[CitationWork] = asyncio.Queue()
        for item in work:
            queue.put_nowait(item)
        effective_domain_limit = domain_limit or ResourceLimiter(
            settings.citation_domain_concurrency,
            resource_type="citation-domain",
        )
        workers = [
            asyncio.create_task(
                citation_worker(
                    queue,
                    writer,
                    effective_domain_limit,
                    settings,
                    resolver,
                    transport,
                    request_limit,
                ),
                name=f"citation-fetch-{index}",
            )
            for index in range(min(settings.citation_fetch_concurrency, len(work)))
        ]
        if workers and not all(await asyncio.gather(*workers)):
            return False
        return await writer.finish_stage()
    finally:
        await writer.stop()
        await writer_task


async def load_citation_work(
    session_factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> list[CitationWork] | None:
    async with session_factory() as session:
        run = await session.get(EvaluationRun, run_id)
        if run is None:
            raise RuntimeError("evaluation run disappeared before citation fetching")
        if run.stage != RunStage.CITATION_FETCH:
            return None
        citations = list(
            await session.scalars(
                select(CitationRecord)
                .join(ExecutionItem, ExecutionItem.id == CitationRecord.execution_item_id)
                .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                .join(Question, Question.id == ExecutionItem.question_id)
                .where(
                    RunAgent.run_id == run_id,
                    Question.requires_citations.is_(True),
                    CitationRecord.fetch_status.in_(
                        [CitationFetchStatus.PENDING, CitationFetchStatus.FETCHING]
                    ),
                )
                .order_by(CitationRecord.id)
            )
        )
    return [
        CitationWork(citation.id, citation.original_url, citation_domain(citation.original_url))
        for citation in citations
    ]


def citation_domain(url: str) -> str:
    try:
        return parse_citation_url(url)[1]
    except BlockedUrlError:
        return "invalid"


async def citation_worker(
    queue: asyncio.Queue[CitationWork],
    writer: CitationWriter,
    domain_limit: ResourceLimiter,
    settings: Settings,
    resolver: Resolver,
    transport: httpx.AsyncBaseTransport | None,
    request_limit: RequestLimit | None = None,
) -> bool:
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            return True
        try:
            if not await writer.start(item.id):
                return False
            async with concurrency_slot(domain_limit, item.domain), concurrency_slot(request_limit):
                result = await fetch_citation(
                    item.url,
                    connect_timeout=settings.citation_connect_timeout_seconds,
                    read_timeout=settings.citation_read_timeout_seconds,
                    max_redirects=settings.citation_max_redirects,
                    max_response_bytes=settings.citation_max_response_bytes,
                    resolver=resolver,
                    transport=transport,
                )
            if not await writer.finish(item.id, result):
                return False
        finally:
            queue.task_done()
