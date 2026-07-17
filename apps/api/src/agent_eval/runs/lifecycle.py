from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from agent_eval.db.enums import (
    AttemptStatus,
    DesiredRunState,
    ExecutionStatus,
    RunAgentStatus,
    RunStage,
    RunStatus,
)
from agent_eval.db.models import EvaluationRun, ExecutionItem, HttpAttempt, RunAgent
from agent_eval.runs.queue import (
    acquire_scheduler_lock,
    database_now,
    has_valid_worker_lease,
    immediate_transaction,
)

TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.PUBLISHED,
    RunStatus.SYSTEM_FAILED,
    RunStatus.CANCELED,
}
COMPLETED_ITEM_STATUSES = {ExecutionStatus.ANSWERED, ExecutionStatus.SCORED}


class RunStateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RecoveryResult:
    run_id: UUID
    abandoned_attempts: int
    reset_items: int
    failed_items: int
    canceled: bool


async def request_run_cancellation(session: AsyncSession, run_id: UUID) -> EvaluationRun:
    run = await session.get(EvaluationRun, run_id)
    if run is None:
        raise RunStateError("run_not_found", "评测任务不存在")
    if run.status == RunStatus.CANCELED:
        return run
    if run.status in TERMINAL_RUN_STATUSES:
        raise RunStateError("run_not_cancelable", "当前状态的评测任务不能取消")
    run.desired_state = DesiredRunState.CANCELED
    await session.flush()
    return run


async def retry_failed_answer_scoring(session: AsyncSession, run_id: UUID) -> EvaluationRun:
    run = await session.get(EvaluationRun, run_id)
    if run is None:
        raise RunStateError("run_not_found", "评测任务不存在")
    if (
        run.status != RunStatus.SYSTEM_FAILED
        or run.stage != RunStage.JUDGING
        or not (run.system_error or "").startswith(
            ("answer_scoring_", "citation_scoring_")
        )
    ):
        raise RunStateError("run_scoring_not_retryable", "当前任务不支持续跑评分")
    run.status = RunStatus.QUEUED
    run.desired_state = DesiredRunState.RUNNING
    run.active_slot = None
    run.claim_owner_id = None
    run.claimed_at = None
    run.heartbeat_at = None
    run.finished_at = None
    run.system_error = None
    await session.flush()
    return run


async def heartbeat_run(
    engine: AsyncEngine,
    owner_id: str,
    run_id: UUID,
    *,
    now: datetime | None = None,
) -> bool:
    async with immediate_transaction(engine) as connection:
        current_time = await database_now(connection, now)
        if not await has_valid_worker_lease(connection, owner_id, current_time):
            return False
        result = await connection.execute(
            update(EvaluationRun)
            .where(
                EvaluationRun.id == run_id,
                EvaluationRun.active_slot.is_not(None),
                EvaluationRun.claim_owner_id == owner_id,
            )
            .values(heartbeat_at=current_time)
        )
        return result.rowcount == 1


async def mark_run_system_failed(
    engine: AsyncEngine,
    owner_id: str,
    run_id: UUID,
    error_type: str,
    *,
    now: datetime | None = None,
) -> bool:
    async with immediate_transaction(engine) as connection:
        current_time = await database_now(connection, now)
        if not await has_valid_worker_lease(connection, owner_id, current_time):
            return False
        result = await connection.execute(
            update(EvaluationRun)
            .where(
                EvaluationRun.id == run_id,
                EvaluationRun.active_slot.is_not(None),
                EvaluationRun.claim_owner_id == owner_id,
            )
            .values(
                status=RunStatus.SYSTEM_FAILED,
                active_slot=None,
                claim_owner_id=None,
                claimed_at=None,
                heartbeat_at=current_time,
                finished_at=current_time,
                system_error=error_type[:500],
            )
        )
        return result.rowcount == 1


async def apply_requested_cancellations(
    engine: AsyncEngine,
    owner_id: str,
    *,
    now: datetime | None = None,
) -> list[UUID]:
    canceled: list[UUID] = []
    async with immediate_transaction(engine) as connection:
        current_time = await database_now(connection, now)
        if not await has_valid_worker_lease(connection, owner_id, current_time):
            return canceled
        await acquire_scheduler_lock(connection)
        statement = (
            select(EvaluationRun.id)
            .where(
                EvaluationRun.desired_state == DesiredRunState.CANCELED,
                EvaluationRun.status.not_in(TERMINAL_RUN_STATUSES),
            )
            .order_by(EvaluationRun.created_at, EvaluationRun.id)
        )
        if engine.dialect.name == "postgresql":
            statement = statement.with_for_update(of=EvaluationRun, skip_locked=True)
        run_ids = list(await connection.scalars(statement))
        for raw_run_id in run_ids:
            run_id = UUID(str(raw_run_id))
            await connection.execute(
                update(HttpAttempt)
                .where(
                    HttpAttempt.id.in_(
                        select(HttpAttempt.id)
                        .join(ExecutionItem, ExecutionItem.id == HttpAttempt.execution_item_id)
                        .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                        .where(
                            RunAgent.run_id == run_id,
                            HttpAttempt.status == AttemptStatus.IN_PROGRESS,
                        )
                    )
                )
                .values(
                    status=AttemptStatus.ABANDONED,
                    finished_at=current_time,
                    error_type="run_canceled",
                )
            )
            await _finalize_cancellation(connection, run_id, current_time)
            canceled.append(run_id)
    return canceled


async def _finalize_cancellation(connection: AsyncConnection, run_id: UUID, now: datetime) -> None:
    await connection.execute(
        update(ExecutionItem)
        .where(
            ExecutionItem.run_agent_id.in_(select(RunAgent.id).where(RunAgent.run_id == run_id)),
            ExecutionItem.status.in_([ExecutionStatus.PENDING, ExecutionStatus.IN_PROGRESS]),
        )
        .values(status=ExecutionStatus.CANCELED, error_type="run_canceled")
    )
    await connection.execute(
        update(RunAgent).where(RunAgent.run_id == run_id).values(status=RunAgentStatus.CANCELED)
    )
    await connection.execute(
        update(EvaluationRun)
        .where(EvaluationRun.id == run_id)
        .values(
            status=RunStatus.CANCELED,
            active_slot=None,
            claim_owner_id=None,
            claimed_at=None,
            heartbeat_at=now,
            finished_at=now,
        )
    )


async def recover_stale_run(
    engine: AsyncEngine,
    owner_id: str,
    *,
    stale_seconds: int = 60,
    now: datetime | None = None,
) -> RecoveryResult | None:
    if stale_seconds <= 0:
        raise ValueError("stale heartbeat threshold must be positive")
    async with immediate_transaction(engine) as connection:
        current_time = await database_now(connection, now)
        stale_before = current_time - timedelta(seconds=stale_seconds)
        if not await has_valid_worker_lease(connection, owner_id, current_time):
            return None
        await acquire_scheduler_lock(connection)
        statement = (
            select(
                EvaluationRun.id,
                EvaluationRun.status,
                EvaluationRun.desired_state,
            )
            .where(
                EvaluationRun.active_slot.is_not(None),
                or_(
                    EvaluationRun.heartbeat_at.is_(None),
                    EvaluationRun.heartbeat_at < stale_before,
                ),
            )
            .order_by(EvaluationRun.heartbeat_at, EvaluationRun.active_slot)
        )
        if engine.dialect.name == "postgresql":
            statement = statement.with_for_update(of=EvaluationRun, skip_locked=True)
        run = (
            (await connection.execute(statement))
            .mappings()
            .first()
        )
        if run is None:
            return None
        run_id = UUID(str(run["id"]))
        attempt_rows = list(
            (
                await connection.execute(
                    select(
                        HttpAttempt.id,
                        HttpAttempt.execution_item_id,
                        HttpAttempt.attempt_no,
                    )
                    .join(ExecutionItem, ExecutionItem.id == HttpAttempt.execution_item_id)
                    .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                    .where(
                        RunAgent.run_id == run_id,
                        HttpAttempt.status == AttemptStatus.IN_PROGRESS,
                    )
                )
            ).mappings()
        )
        attempt_numbers: dict[UUID, int] = {}
        for row in attempt_rows:
            item_id = UUID(str(row["execution_item_id"]))
            attempt_numbers[item_id] = max(attempt_numbers.get(item_id, 0), row["attempt_no"])
        if attempt_rows:
            await connection.execute(
                update(HttpAttempt)
                .where(HttpAttempt.id.in_([row["id"] for row in attempt_rows]))
                .values(
                    status=AttemptStatus.ABANDONED,
                    finished_at=current_time,
                    error_type="worker_restart",
                )
            )

        item_rows = list(
            (
                await connection.execute(
                    select(
                        ExecutionItem.id,
                        ExecutionItem.run_agent_id,
                        ExecutionItem.attempt_count,
                    )
                    .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                    .where(
                        RunAgent.run_id == run_id,
                        or_(
                            ExecutionItem.status == ExecutionStatus.IN_PROGRESS,
                            ExecutionItem.id.in_(attempt_numbers),
                        ),
                    )
                )
            ).mappings()
        )
        reset_items = 0
        failed_items = 0
        for item in item_rows:
            item_id = UUID(str(item["id"]))
            attempt_count = max(item["attempt_count"], attempt_numbers.get(item_id, 0))
            if attempt_count >= 2:
                item_status = ExecutionStatus.FAILED
                error_type = "worker_restart_after_retry"
                failed_items += 1
            else:
                item_status = ExecutionStatus.PENDING
                error_type = "worker_restart_retry"
                reset_items += 1
            await connection.execute(
                update(ExecutionItem)
                .where(ExecutionItem.id == item_id)
                .values(
                    status=item_status,
                    attempt_count=attempt_count,
                    error_type=error_type,
                )
            )

        await _recompute_run_agent_counts(connection, run_id)
        canceled = run["desired_state"] == DesiredRunState.CANCELED
        if canceled:
            await _finalize_cancellation(connection, run_id, current_time)
        else:
            await connection.execute(
                update(EvaluationRun)
                .where(EvaluationRun.id == run_id)
                .values(
                    claim_owner_id=owner_id,
                    claimed_at=current_time,
                    heartbeat_at=current_time,
                )
            )
        return RecoveryResult(
            run_id=run_id,
            abandoned_attempts=len(attempt_rows),
            reset_items=reset_items,
            failed_items=failed_items,
            canceled=canceled,
        )


async def _recompute_run_agent_counts(connection: AsyncConnection, run_id: UUID) -> None:
    run_agent_ids = list(
        await connection.scalars(select(RunAgent.id).where(RunAgent.run_id == run_id))
    )
    for run_agent_id in run_agent_ids:
        rows = list(
            (
                await connection.execute(
                    select(ExecutionItem.status, ExecutionItem.attempt_count).where(
                        ExecutionItem.run_agent_id == run_agent_id
                    )
                )
            ).all()
        )
        await connection.execute(
            update(RunAgent)
            .where(RunAgent.id == run_agent_id)
            .values(
                completed=sum(status in COMPLETED_ITEM_STATUSES for status, _ in rows),
                failed=sum(status == ExecutionStatus.FAILED for status, _ in rows),
                retried=sum(attempt_count >= 2 for _, attempt_count in rows),
                status=(
                    RunAgentStatus.COMPLETED
                    if all(
                        status in COMPLETED_ITEM_STATUSES | {ExecutionStatus.FAILED}
                        for status, _ in rows
                    )
                    else RunAgentStatus.RUNNING
                ),
            )
        )
