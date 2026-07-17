import asyncio
import logging
import os
import signal
import socket
from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agent_eval.auth.encryption import TokenCipher
from agent_eval.core.artifact_store import ArtifactStore, create_artifact_store
from agent_eval.core.logging import configure_logging
from agent_eval.core.settings import Settings
from agent_eval.core.storage import ensure_storage
from agent_eval.db.engine import create_engine, verify_database
from agent_eval.db.enums import CitationFetchStatus, ExecutionStatus, RunStage, RunStatus
from agent_eval.db.models import (
    CitationRecord,
    EvaluationRun,
    ExecutionItem,
    RunAgent,
    ScoreRecord,
)
from agent_eval.db.session import create_session_factory
from agent_eval.leaderboards.publisher import execute_aggregation_and_publish
from agent_eval.runs.lifecycle import (
    apply_requested_cancellations,
    heartbeat_run,
    mark_run_system_failed,
    recover_stale_run,
)
from agent_eval.runs.queue import (
    acquire_worker_lease,
    claim_next_run,
    release_worker_lease,
    worker_owner_id,
)
from agent_eval.worker.citation_scoring import execute_citation_scoring
from agent_eval.worker.citations import execute_citation_fetch
from agent_eval.worker.executor import execute_run_inference
from agent_eval.worker.limits import WorkerLimits
from agent_eval.worker.scoring import execute_answer_scoring

logger = logging.getLogger(__name__)
RunPhase = Literal[
    "inference",
    "answer_scoring",
    "citation_fetch",
    "citation_scoring",
    "aggregation",
]


async def resume_phase(
    session_factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> RunPhase:
    async with session_factory() as session:
        run = await session.get(EvaluationRun, run_id)
        if run is None:
            raise RuntimeError("evaluation run disappeared before processing")
        if run.status == RunStatus.RUNNING:
            return "inference"
        if run.status != RunStatus.SCORING:
            raise RuntimeError(f"evaluation run cannot resume from {run.status.value}")
        if run.stage == RunStage.CITATION_FETCH:
            return "citation_fetch"
        if run.stage in {
            RunStage.AGGREGATING,
            RunStage.READY_TO_PUBLISH,
            RunStage.PUBLISHING,
        }:
            return "aggregation"
        if run.stage != RunStage.JUDGING:
            raise RuntimeError(f"evaluation run cannot resume from {run.stage.value}")

        unscored_item = await session.scalar(
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
        if unscored_item is not None:
            return "answer_scoring"
        pending_citation = await session.scalar(
            select(CitationRecord.id)
            .join(ExecutionItem, ExecutionItem.id == CitationRecord.execution_item_id)
            .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
            .where(
                RunAgent.run_id == run_id,
                CitationRecord.fetch_status.in_(
                    [CitationFetchStatus.PENDING, CitationFetchStatus.FETCHING]
                ),
            )
            .limit(1)
        )
        if pending_citation is not None:
            return "citation_fetch"
        unscored_citation = await session.scalar(
            select(CitationRecord.id)
            .join(ExecutionItem, ExecutionItem.id == CitationRecord.execution_item_id)
            .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
            .where(
                RunAgent.run_id == run_id,
                CitationRecord.support_score.is_(None),
            )
            .limit(1)
        )
        return "citation_scoring" if unscored_citation is not None else "aggregation"


async def process_run(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    run_id: UUID,
    *,
    artifact_store: ArtifactStore,
    token_cipher: TokenCipher | None,
    owner_id: str,
    limits: WorkerLimits,
    initial_phase: RunPhase | None = None,
) -> bool:
    try:
        phase = initial_phase or await resume_phase(session_factory, run_id)
    except Exception as error:
        logger.error(
            "run_processing_failed",
            extra={
                "run_id": run_id,
                "phase": "resume",
                "error_type": type(error).__name__,
            },
        )
        await mark_run_system_failed(
            engine,
            owner_id,
            run_id,
            f"resume_{type(error).__name__}",
        )
        return False
    while True:
        try:
            if phase == "inference":
                phase_complete = await execute_run_inference(
                    session_factory,
                    settings,
                    run_id,
                    artifact_store=artifact_store,
                    token_cipher=token_cipher,
                    owner_id=owner_id,
                    request_limit=limits.agent_requests,
                )
            elif phase == "answer_scoring":
                phase_complete = await execute_answer_scoring(
                    session_factory,
                    settings,
                    run_id,
                    artifact_store=artifact_store,
                    owner_id=owner_id,
                    request_limit=limits.judge_requests,
                )
            elif phase == "citation_fetch":
                phase_complete = await execute_citation_fetch(
                    session_factory,
                    settings,
                    run_id,
                    artifact_store=artifact_store,
                    owner_id=owner_id,
                    request_limit=limits.citation_fetches,
                    domain_limit=limits.citation_domains,
                )
            elif phase == "citation_scoring":
                phase_complete = await execute_citation_scoring(
                    session_factory,
                    settings,
                    run_id,
                    artifact_store=artifact_store,
                    owner_id=owner_id,
                    request_limit=limits.judge_requests,
                )
            else:
                async with limits.publishing:
                    phase_complete = await execute_aggregation_and_publish(
                        session_factory,
                        run_id,
                        owner_id=owner_id,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            detail = getattr(error, "error_type", type(error).__name__)
            logger.error(
                "run_processing_failed",
                extra={
                    "run_id": run_id,
                    "phase": phase,
                    "error_type": type(error).__name__,
                },
            )
            await mark_run_system_failed(
                engine,
                owner_id,
                run_id,
                f"{phase}_{detail}",
            )
            return False

        logger.info(
            "run_phase_finished",
            extra={"run_id": run_id, "phase": phase, "complete": phase_complete},
        )
        if not phase_complete:
            return False
        if phase == "aggregation":
            return True
        if phase == "inference":
            phase = "answer_scoring"
        elif phase == "answer_scoring":
            phase = "citation_fetch"
        elif phase == "citation_fetch":
            phase = "citation_scoring"
        else:
            phase = "aggregation"


async def cancel_run_tasks(
    run_tasks: dict[UUID, asyncio.Task[bool]], run_ids: set[UUID] | None = None
) -> None:
    selected_ids = set(run_tasks) if run_ids is None else set(run_tasks).intersection(run_ids)
    tasks = [run_tasks.pop(run_id) for run_id in selected_ids]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_worker(
    settings: Settings | None = None, artifact_store: ArtifactStore | None = None
) -> None:
    worker_settings = settings or Settings()
    worker_artifact_store = artifact_store or create_artifact_store(worker_settings)
    worker_settings.require_worker_runtime()
    configure_logging(worker_settings.log_level)
    ensure_storage(worker_settings)
    await asyncio.to_thread(worker_artifact_store.verify_writable)
    engine = create_engine(worker_settings)
    session_factory = create_session_factory(engine)
    owner_id = worker_owner_id(socket.gethostname(), os.getpid(), uuid4())
    limits = WorkerLimits.from_settings(worker_settings, engine=engine, owner_id=owner_id)
    token_cipher = (
        TokenCipher.from_secret(worker_settings.encryption_key)
        if worker_settings.encryption_key is not None
        else None
    )
    stop_event = asyncio.Event()
    owns_lease = False
    next_lease_renewal = 0.0
    run_tasks: dict[UUID, asyncio.Task[bool]] = {}

    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop_event.set)

    try:
        await verify_database(engine)
        logger.info("worker_ready", extra={"owner_id": owner_id})
        while not stop_event.is_set():
            if monotonic() >= next_lease_renewal:
                acquired = await acquire_worker_lease(
                    engine,
                    owner_id,
                    lease_seconds=worker_settings.worker_lease_seconds,
                )
                if acquired and not owns_lease:
                    logger.info("worker_lease_acquired", extra={"owner_id": owner_id})
                elif not acquired and owns_lease:
                    logger.warning("worker_lease_lost", extra={"owner_id": owner_id})
                    await cancel_run_tasks(run_tasks)
                owns_lease = acquired
                next_lease_renewal = monotonic() + worker_settings.worker_heartbeat_seconds
                if owns_lease:
                    failed_heartbeats = {
                        run_id
                        for run_id, task in run_tasks.items()
                        if not task.done()
                        and not await heartbeat_run(engine, owner_id, run_id)
                    }
                    for run_id in failed_heartbeats:
                        logger.warning("run_heartbeat_failed", extra={"run_id": run_id})
                    await cancel_run_tasks(run_tasks, failed_heartbeats)
            if owns_lease:
                canceled_run_ids = await apply_requested_cancellations(engine, owner_id)
                for run_id in canceled_run_ids:
                    logger.info("run_canceled", extra={"run_id": run_id})
                await cancel_run_tasks(run_tasks, set(canceled_run_ids))

                for run_id, task in list(run_tasks.items()):
                    if not task.done():
                        continue
                    del run_tasks[run_id]
                    try:
                        completed = task.result()
                    except asyncio.CancelledError:
                        continue
                    except Exception as error:
                        logger.error(
                            "run_task_failed",
                            extra={"run_id": run_id, "error_type": type(error).__name__},
                        )
                    else:
                        logger.info(
                            "run_processing_finished",
                            extra={"run_id": run_id, "complete": completed},
                        )

                while len(run_tasks) < worker_settings.worker_max_local_runs:
                    recovery = await recover_stale_run(
                        engine,
                        owner_id,
                        stale_seconds=worker_settings.worker_stale_run_seconds,
                    )
                    if recovery is not None:
                        logger.info(
                            "run_recovered",
                            extra={
                                "run_id": recovery.run_id,
                                "abandoned_attempts": recovery.abandoned_attempts,
                            },
                        )
                        if not recovery.canceled:
                            run_tasks[recovery.run_id] = asyncio.create_task(
                                process_run(
                                    engine,
                                    session_factory,
                                    worker_settings,
                                    recovery.run_id,
                                    artifact_store=worker_artifact_store,
                                    token_cipher=token_cipher,
                                    owner_id=owner_id,
                                    limits=limits,
                                ),
                                name=f"run-pipeline-{recovery.run_id}",
                            )
                            continue
                    claimed_run_id = await claim_next_run(
                        engine,
                        owner_id,
                        max_active_runs=worker_settings.worker_max_active_runs,
                        max_provider_runs=worker_settings.worker_max_provider_runs,
                    )
                    if claimed_run_id is None:
                        break
                    logger.info("run_claimed", extra={"run_id": claimed_run_id})
                    run_tasks[claimed_run_id] = asyncio.create_task(
                        process_run(
                            engine,
                            session_factory,
                            worker_settings,
                            claimed_run_id,
                            artifact_store=worker_artifact_store,
                            token_cipher=token_cipher,
                            owner_id=owner_id,
                            limits=limits,
                            initial_phase="inference",
                        ),
                        name=f"run-pipeline-{claimed_run_id}",
                    )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=worker_settings.worker_poll_seconds
                )
            except TimeoutError:
                logger.debug("worker_poll")
    finally:
        await cancel_run_tasks(run_tasks)
        if owns_lease:
            try:
                await release_worker_lease(engine, owner_id)
            except Exception as error:
                logger.error(
                    "worker_lease_release_failed",
                    extra={"owner_id": owner_id, "error_type": type(error).__name__},
                )
        await engine.dispose()
        logger.info("worker_stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
