from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_eval.db.enums import WorkspaceAttemptStatus
from agent_eval.db.models import WorkspaceAttempt
from agent_eval.runs.queue import (
    database_now,
    has_valid_worker_lease,
    immediate_transaction,
)


@dataclass(frozen=True)
class WorkspaceClaim:
    attempt_id: UUID
    claim_token: str


async def fail_stale_workspace_attempts(
    engine: AsyncEngine, *, now: datetime | None = None
) -> list[UUID]:
    finalized_ids: list[UUID] = []
    async with immediate_transaction(engine) as connection:
        current_time = await database_now(connection, now)
        statement = (
            select(WorkspaceAttempt.id)
            .where(
                WorkspaceAttempt.status == WorkspaceAttemptStatus.RUNNING,
                WorkspaceAttempt.lease_expires_at <= current_time,
            )
            .order_by(WorkspaceAttempt.lease_expires_at, WorkspaceAttempt.id)
        )
        if engine.dialect.name == "postgresql":
            statement = statement.with_for_update(of=WorkspaceAttempt, skip_locked=True)
        rows = list(
            (
                await connection.execute(
                    statement.with_only_columns(
                        WorkspaceAttempt.id,
                        WorkspaceAttempt.cancel_requested_at,
                    )
                )
            ).all()
        )
        if rows:
            canceled_ids = [row.id for row in rows if row.cancel_requested_at is not None]
            failed_raw_ids = [row.id for row in rows if row.cancel_requested_at is None]
            if canceled_ids:
                await connection.execute(
                    update(WorkspaceAttempt)
                    .where(
                        WorkspaceAttempt.id.in_(canceled_ids),
                        WorkspaceAttempt.status == WorkspaceAttemptStatus.RUNNING,
                        WorkspaceAttempt.lease_expires_at <= current_time,
                        WorkspaceAttempt.cancel_requested_at.is_not(None),
                    )
                    .values(
                        status=WorkspaceAttemptStatus.CANCELED,
                        claim_owner_id=None,
                        claim_token=None,
                        lease_expires_at=None,
                        heartbeat_at=None,
                        finished_at=current_time,
                        error_type="workspace_canceled",
                        error_category="canceled",
                        updated_at=current_time,
                    )
                )
            if failed_raw_ids:
                await connection.execute(
                    update(WorkspaceAttempt)
                    .where(
                        WorkspaceAttempt.id.in_(failed_raw_ids),
                        WorkspaceAttempt.status == WorkspaceAttemptStatus.RUNNING,
                        WorkspaceAttempt.lease_expires_at <= current_time,
                        WorkspaceAttempt.cancel_requested_at.is_(None),
                    )
                    .values(
                        status=WorkspaceAttemptStatus.FAILED,
                        claim_owner_id=None,
                        claim_token=None,
                        lease_expires_at=None,
                        heartbeat_at=None,
                        finished_at=current_time,
                        error_type="worker_lost",
                        error_category="worker_lost",
                        updated_at=current_time,
                    )
                )
            finalized_ids = [UUID(str(row.id)) for row in rows]
    return finalized_ids


async def claim_next_workspace_attempt(
    engine: AsyncEngine,
    owner_id: str,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> WorkspaceClaim | None:
    if lease_seconds <= 0:
        raise ValueError("workspace lease duration must be positive")
    async with immediate_transaction(engine) as connection:
        current_time = await database_now(connection, now)
        if not await has_valid_worker_lease(connection, owner_id, current_time):
            return None
        statement = (
            select(WorkspaceAttempt.id)
            .where(WorkspaceAttempt.status == WorkspaceAttemptStatus.QUEUED)
            .order_by(WorkspaceAttempt.created_at, WorkspaceAttempt.id)
            .limit(1)
        )
        if engine.dialect.name == "postgresql":
            statement = statement.with_for_update(of=WorkspaceAttempt, skip_locked=True)
        raw_attempt_id = await connection.scalar(statement)
        if raw_attempt_id is None:
            return None
        token = uuid4().hex
        result = await connection.execute(
            update(WorkspaceAttempt)
            .where(
                WorkspaceAttempt.id == raw_attempt_id,
                WorkspaceAttempt.status == WorkspaceAttemptStatus.QUEUED,
            )
            .values(
                status=WorkspaceAttemptStatus.RUNNING,
                claim_owner_id=owner_id,
                claim_token=token,
                claimed_at=current_time,
                lease_expires_at=current_time + timedelta(seconds=lease_seconds),
                heartbeat_at=current_time,
                started_at=current_time,
                updated_at=current_time,
            )
        )
        if result.rowcount != 1:
            return None
        return WorkspaceClaim(UUID(str(raw_attempt_id)), token)


async def heartbeat_workspace_attempt(
    engine: AsyncEngine,
    owner_id: str,
    claim: WorkspaceClaim,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    async with immediate_transaction(engine) as connection:
        current_time = await database_now(connection, now)
        if not await has_valid_worker_lease(connection, owner_id, current_time):
            return False
        result = await connection.execute(
            update(WorkspaceAttempt)
            .where(
                WorkspaceAttempt.id == claim.attempt_id,
                WorkspaceAttempt.status == WorkspaceAttemptStatus.RUNNING,
                WorkspaceAttempt.claim_owner_id == owner_id,
                WorkspaceAttempt.claim_token == claim.claim_token,
                WorkspaceAttempt.lease_expires_at > func.current_timestamp(),
            )
            .values(
                heartbeat_at=current_time,
                lease_expires_at=current_time + timedelta(seconds=lease_seconds),
                updated_at=current_time,
            )
        )
        return result.rowcount == 1
