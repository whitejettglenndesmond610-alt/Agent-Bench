from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from agent_eval.db.base import ensure_utc
from agent_eval.db.enums import DesiredRunState, RunAgentStatus, RunStage, RunStatus
from agent_eval.db.models import Agent, AgentVersion, EvaluationRun, RunAgent, WorkerLease

# Stable, application-specific key used only for transaction-scoped PostgreSQL locks.
SCHEDULER_ADVISORY_LOCK_KEY = 0x4147454E54525631


@asynccontextmanager
async def immediate_transaction(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        if engine.dialect.name == "sqlite":
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            await connection.begin()
        try:
            yield connection
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise


async def acquire_scheduler_lock(connection: AsyncConnection | AsyncSession) -> None:
    dialect_name = (
        connection.get_bind().dialect.name
        if isinstance(connection, AsyncSession)
        else connection.dialect.name
    )
    if dialect_name == "postgresql":
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": SCHEDULER_ADVISORY_LOCK_KEY},
        )


async def database_now(
    connection: AsyncConnection | AsyncSession, now: datetime | None = None
) -> datetime:
    if now is not None:
        return ensure_utc(now)
    value = await connection.scalar(select(func.current_timestamp()))
    if value is None:
        raise RuntimeError("database did not return its current time")
    return ensure_utc(value)


async def has_valid_worker_lease(
    connection: AsyncConnection, owner_id: str, now: datetime | None = None
) -> bool:
    current_time = await database_now(connection, now)
    lease_table = WorkerLease.__table__
    lease = (
        (
            await connection.execute(
                select(lease_table.c.lease_expires_at).where(
                    lease_table.c.name == owner_id,
                    lease_table.c.owner_id == owner_id,
                )
            )
        )
        .mappings()
        .first()
    )
    return lease is not None and ensure_utc(lease["lease_expires_at"]) > current_time


async def session_has_valid_worker_lease(
    session: AsyncSession, owner_id: str, now: datetime | None = None
) -> bool:
    current_time = await database_now(session, now)
    lease = await session.get(WorkerLease, owner_id, populate_existing=True)
    return (
        lease is not None
        and lease.owner_id == owner_id
        and ensure_utc(lease.lease_expires_at) > current_time
    )


async def lock_owned_run(
    session: AsyncSession, run_id: UUID, owner_id: str
) -> EvaluationRun | None:
    if not owner_id:
        raise ValueError("worker owner ID is required")
    run = await session.scalar(
        select(EvaluationRun)
        .where(EvaluationRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        run is None
        or run.claim_owner_id != owner_id
        or not await session_has_valid_worker_lease(session, owner_id)
    ):
        return None
    return run


async def acquire_worker_lease(
    engine: AsyncEngine,
    owner_id: str,
    *,
    lease_seconds: int = 30,
    now: datetime | None = None,
) -> bool:
    if not owner_id or len(owner_id) > 120:
        raise ValueError("worker owner ID must contain 1 to 120 characters")
    if lease_seconds <= 0:
        raise ValueError("lease duration must be positive")
    async with immediate_transaction(engine) as connection:
        current_time = await database_now(connection, now)
        await connection.execute(
            delete(WorkerLease).where(
                WorkerLease.name != owner_id,
                WorkerLease.lease_expires_at <= current_time,
            )
        )
        values = {
            "name": owner_id,
            "owner_id": owner_id,
            "lease_expires_at": current_time + timedelta(seconds=lease_seconds),
            "heartbeat_at": current_time,
        }
        insert_factory = (
            postgresql_insert if engine.dialect.name == "postgresql" else sqlite_insert
        )
        statement = insert_factory(WorkerLease).values(**values)
        await connection.execute(
            statement.on_conflict_do_update(
                index_elements=[WorkerLease.name],
                set_={key: value for key, value in values.items() if key != "name"},
            )
        )
        return True


async def release_worker_lease(engine: AsyncEngine, owner_id: str) -> bool:
    async with immediate_transaction(engine) as connection:
        result = await connection.execute(
            delete(WorkerLease).where(
                WorkerLease.name == owner_id,
                WorkerLease.owner_id == owner_id,
            )
        )
        return bool(result.rowcount)


async def claim_next_run(
    engine: AsyncEngine,
    owner_id: str,
    *,
    max_active_runs: int = 3,
    max_provider_runs: int = 2,
    now: datetime | None = None,
) -> UUID | None:
    if not 1 <= max_active_runs <= 3:
        raise ValueError("active run limit must be between 1 and 3")
    if not 1 <= max_provider_runs <= max_active_runs:
        raise ValueError("provider run limit must be between 1 and active run limit")
    run_table = EvaluationRun.__table__
    run_agent_table = RunAgent.__table__
    agent_version_table = AgentVersion.__table__
    agent_table = Agent.__table__

    async with immediate_transaction(engine) as connection:
        current_time = await database_now(connection, now)
        if not await has_valid_worker_lease(connection, owner_id, current_time):
            return None
        await acquire_scheduler_lock(connection)

        active_runs = list(
            (
                await connection.execute(
                    select(run_table.c.id, run_table.c.active_slot).where(
                        run_table.c.active_slot.is_not(None)
                    )
                )
            ).mappings()
        )
        if len(active_runs) >= max_active_runs:
            return None
        occupied_slots = {row["active_slot"] for row in active_runs}
        available_slot = next(
            (slot for slot in range(1, max_active_runs + 1) if slot not in occupied_slots),
            None,
        )
        if available_slot is None:
            return None

        active_rows = list(
            (
                await connection.execute(
                    select(
                        run_table.c.id,
                        run_agent_table.c.agent_version_id,
                        agent_table.c.provider,
                    )
                    .join(run_agent_table, run_agent_table.c.run_id == run_table.c.id)
                    .join(
                        agent_version_table,
                        agent_version_table.c.id == run_agent_table.c.agent_version_id,
                    )
                    .join(agent_table, agent_table.c.id == agent_version_table.c.agent_id)
                    .where(run_table.c.active_slot.is_not(None))
                )
            ).mappings()
        )
        active_versions = {row["agent_version_id"] for row in active_rows}
        active_provider_runs: dict[str, set[object]] = {}
        for row in active_rows:
            active_provider_runs.setdefault(row["provider"], set()).add(row["id"])

        queued_statement = (
            select(run_table.c.id)
            .where(
                run_table.c.status == RunStatus.QUEUED,
                run_table.c.desired_state == DesiredRunState.RUNNING,
            )
            .order_by(run_table.c.created_at, run_table.c.id)
        )
        if engine.dialect.name == "postgresql":
            queued_statement = queued_statement.with_for_update(
                of=run_table, skip_locked=True
            )
        queued_ids = list(await connection.scalars(queued_statement))
        if not queued_ids:
            return None
        queued_rows = list(
            (
                await connection.execute(
                    select(
                        run_table.c.id,
                        run_agent_table.c.agent_version_id,
                        agent_table.c.provider,
                    )
                    .join(run_agent_table, run_agent_table.c.run_id == run_table.c.id)
                    .join(
                        agent_version_table,
                        agent_version_table.c.id == run_agent_table.c.agent_version_id,
                    )
                    .join(agent_table, agent_table.c.id == agent_version_table.c.agent_id)
                    .where(run_table.c.id.in_(queued_ids))
                    .order_by(run_table.c.created_at, run_table.c.id, run_agent_table.c.id)
                )
            ).mappings()
        )
        queued_resources: dict[object, tuple[set[object], set[str]]] = {}
        for row in queued_rows:
            versions, providers = queued_resources.setdefault(row["id"], (set(), set()))
            versions.add(row["agent_version_id"])
            providers.add(row["provider"])

        run_id = next(
            (
                candidate_id
                for candidate_id, (versions, providers) in queued_resources.items()
                if versions.isdisjoint(active_versions)
                and all(
                    len(active_provider_runs.get(provider, set())) < max_provider_runs
                    for provider in providers
                )
            ),
            None,
        )
        if run_id is None:
            return None

        result = await connection.execute(
            update(EvaluationRun)
            .where(run_table.c.id == run_id, run_table.c.status == RunStatus.QUEUED)
            .values(
                status=RunStatus.RUNNING,
                stage=RunStage.INFERENCE,
                active_slot=available_slot,
                claim_owner_id=owner_id,
                claimed_at=current_time,
                heartbeat_at=current_time,
                started_at=func.coalesce(run_table.c.started_at, current_time),
            )
        )
        if result.rowcount != 1:
            return None
        await connection.execute(
            update(RunAgent)
            .where(
                run_agent_table.c.run_id == run_id,
                run_agent_table.c.status != RunAgentStatus.COMPLETED,
            )
            .values(status=RunAgentStatus.RUNNING)
        )
        return UUID(str(run_id))


def worker_owner_id(hostname: str, process_id: int, instance_id: UUID) -> str:
    return f"{hostname[:60]}:{process_id}:{instance_id}"
