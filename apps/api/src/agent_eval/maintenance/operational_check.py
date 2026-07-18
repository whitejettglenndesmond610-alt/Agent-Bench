import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from agent_eval.core.artifact_store import ArtifactStore, create_artifact_store
from agent_eval.core.settings import Settings
from agent_eval.db.base import ensure_utc
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import ResultValidityStatus, RunStatus, WorkspaceAttemptStatus
from agent_eval.db.models import (
    ArtifactIndex,
    EvaluationRun,
    ResourceLease,
    RunAgent,
    WorkerLease,
    WorkspaceAttempt,
)
from agent_eval.db.session import create_session_factory

ACTIVE_RUN_STATUSES = (RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.SCORING)


class OperationalCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class OperationalStatus:
    database_revision: str
    database_head: str
    queued_runs: int
    oldest_queue_age_seconds: int
    active_runs: int
    stale_active_runs: int
    valid_worker_leases: int
    expired_worker_leases: int
    active_resource_leases: int
    expired_resource_leases: int
    recent_published_run_agents: int
    recent_system_failed_run_agents: int
    recent_system_failed_runs: int
    recent_invalid_run_agents: int
    artifact_index_count: int
    artifact_store_writable: bool
    workspace_queued_attempts: int = 0
    workspace_running_attempts: int = 0
    workspace_failed_attempts: int = 0
    workspace_stale_attempts: int = 0


async def collect_operational_status(
    session: AsyncSession,
    settings: Settings,
    *,
    database_head: str,
) -> OperationalStatus:
    current_timestamp = await session.scalar(select(func.current_timestamp()))
    if current_timestamp is None:
        raise OperationalCheckError("database time is unavailable")
    database_now = ensure_utc(current_timestamp)
    revision = await session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
    if not isinstance(revision, str):
        raise OperationalCheckError("database revision is unavailable")

    queue_count, oldest_queued_at = (
        await session.execute(
            select(func.count(EvaluationRun.id), func.min(EvaluationRun.created_at)).where(
                EvaluationRun.status == RunStatus.QUEUED
            )
        )
    ).one()
    stale_cutoff = database_now - timedelta(seconds=settings.worker_stale_run_seconds)
    window_cutoff = database_now - timedelta(hours=settings.monitoring_window_hours)
    resource_expiry_cutoff = database_now - timedelta(
        seconds=settings.monitoring_resource_lease_grace_seconds
    )

    async def count(statement: Select[tuple[int]]) -> int:
        value = await session.scalar(statement)
        return int(value or 0)

    active_runs = await count(
        select(func.count(EvaluationRun.id)).where(EvaluationRun.status.in_(ACTIVE_RUN_STATUSES))
    )
    stale_active_runs = await count(
        select(func.count(EvaluationRun.id)).where(
            EvaluationRun.status.in_(ACTIVE_RUN_STATUSES),
            or_(EvaluationRun.heartbeat_at.is_(None), EvaluationRun.heartbeat_at < stale_cutoff),
        )
    )
    valid_workers = await count(
        select(func.count())
        .select_from(WorkerLease)
        .where(WorkerLease.lease_expires_at > database_now)
    )
    expired_workers = await count(
        select(func.count())
        .select_from(WorkerLease)
        .where(WorkerLease.lease_expires_at <= database_now)
    )
    active_resources = await count(
        select(func.count())
        .select_from(ResourceLease)
        .where(ResourceLease.lease_expires_at > database_now)
    )
    expired_resources = await count(
        select(func.count())
        .select_from(ResourceLease)
        .where(ResourceLease.lease_expires_at <= resource_expiry_cutoff)
    )

    recent_base = EvaluationRun.finished_at >= window_cutoff
    published_agents = await count(
        select(func.count(RunAgent.id))
        .join(EvaluationRun, EvaluationRun.id == RunAgent.run_id)
        .where(EvaluationRun.status == RunStatus.PUBLISHED, recent_base)
    )
    failed_agents = await count(
        select(func.count(RunAgent.id))
        .join(EvaluationRun, EvaluationRun.id == RunAgent.run_id)
        .where(EvaluationRun.status == RunStatus.SYSTEM_FAILED, recent_base)
    )
    failed_runs = await count(
        select(func.count(EvaluationRun.id)).where(
            EvaluationRun.status == RunStatus.SYSTEM_FAILED, recent_base
        )
    )
    invalid_agents = await count(
        select(func.count(RunAgent.id))
        .join(EvaluationRun, EvaluationRun.id == RunAgent.run_id)
        .where(RunAgent.validity_status == ResultValidityStatus.INVALID, recent_base)
    )
    artifact_count = await count(select(func.count()).select_from(ArtifactIndex))
    workspace_queued = await count(
        select(func.count(WorkspaceAttempt.id)).where(
            WorkspaceAttempt.status == WorkspaceAttemptStatus.QUEUED
        )
    )
    workspace_running = await count(
        select(func.count(WorkspaceAttempt.id)).where(
            WorkspaceAttempt.status == WorkspaceAttemptStatus.RUNNING
        )
    )
    workspace_failed = await count(
        select(func.count(WorkspaceAttempt.id)).where(
            WorkspaceAttempt.status == WorkspaceAttemptStatus.FAILED
        )
    )
    workspace_stale = await count(
        select(func.count(WorkspaceAttempt.id)).where(
            WorkspaceAttempt.status == WorkspaceAttemptStatus.RUNNING,
            WorkspaceAttempt.lease_expires_at <= database_now,
        )
    )
    oldest_age = 0
    if oldest_queued_at is not None:
        oldest_age = max(0, int((database_now - ensure_utc(oldest_queued_at)).total_seconds()))

    return OperationalStatus(
        database_revision=revision,
        database_head=database_head,
        queued_runs=int(queue_count),
        oldest_queue_age_seconds=oldest_age,
        active_runs=active_runs,
        stale_active_runs=stale_active_runs,
        valid_worker_leases=valid_workers,
        expired_worker_leases=expired_workers,
        active_resource_leases=active_resources,
        expired_resource_leases=expired_resources,
        recent_published_run_agents=published_agents,
        recent_system_failed_run_agents=failed_agents,
        recent_system_failed_runs=failed_runs,
        recent_invalid_run_agents=invalid_agents,
        artifact_index_count=artifact_count,
        artifact_store_writable=False,
        workspace_queued_attempts=workspace_queued,
        workspace_running_attempts=workspace_running,
        workspace_failed_attempts=workspace_failed,
        workspace_stale_attempts=workspace_stale,
    )


def alert_reasons(status: OperationalStatus, settings: Settings) -> list[str]:
    reasons: list[str] = []
    if status.database_revision != status.database_head:
        reasons.append("database revision is not at migration head")
    if status.valid_worker_leases < settings.monitoring_expected_workers:
        reasons.append("valid worker leases are below expected")
    if status.stale_active_runs > 0:
        reasons.append("active runs have stale heartbeats")
    if status.workspace_stale_attempts > 0:
        reasons.append("workspace attempts have stale leases")
    if status.queued_runs > settings.monitoring_max_queued_runs:
        reasons.append("queued runs exceed threshold")
    if status.oldest_queue_age_seconds > settings.monitoring_max_queue_age_minutes * 60:
        reasons.append("oldest queued run exceeds age threshold")
    if status.expired_resource_leases > settings.monitoring_max_expired_resource_leases:
        reasons.append("expired resource leases exceed threshold after grace")
    if status.recent_system_failed_runs > settings.monitoring_max_system_failed_runs:
        reasons.append("recent system-failed runs exceed threshold")
    if not status.artifact_store_writable:
        reasons.append("artifact store is not writable")
    return reasons


def render_status(status: OperationalStatus, window_hours: int) -> str:
    values = (
        ("database_revision", status.database_revision),
        ("database_head", status.database_head),
        ("queued_runs", status.queued_runs),
        ("oldest_queue_age_seconds", status.oldest_queue_age_seconds),
        ("active_runs", status.active_runs),
        ("stale_active_runs", status.stale_active_runs),
        ("valid_worker_leases", status.valid_worker_leases),
        ("expired_worker_leases", status.expired_worker_leases),
        ("active_resource_leases", status.active_resource_leases),
        ("expired_resource_leases_after_grace", status.expired_resource_leases),
        (f"published_run_agents_{window_hours}h", status.recent_published_run_agents),
        (f"system_failed_run_agents_{window_hours}h", status.recent_system_failed_run_agents),
        (f"system_failed_runs_{window_hours}h", status.recent_system_failed_runs),
        (f"invalid_run_agents_{window_hours}h", status.recent_invalid_run_agents),
        ("artifact_index_count", status.artifact_index_count),
        ("artifact_store_writable", str(status.artifact_store_writable).lower()),
        ("workspace_queued_attempts", status.workspace_queued_attempts),
        ("workspace_running_attempts", status.workspace_running_attempts),
        ("workspace_failed_attempts", status.workspace_failed_attempts),
        ("workspace_stale_attempts", status.workspace_stale_attempts),
    )
    return "\n".join(f"{key}={value}" for key, value in values)


async def operational_check(
    settings: Settings | None = None,
    artifact_store: ArtifactStore | None = None,
) -> None:
    settings = settings or Settings()
    store = artifact_store or create_artifact_store(settings)
    try:
        head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        if head is None:
            raise OperationalCheckError("migration head is unavailable")
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        try:
            async with session_factory() as session:
                status = await collect_operational_status(session, settings, database_head=head)
        finally:
            await engine.dispose()
    except OperationalCheckError:
        raise
    except Exception as error:
        raise OperationalCheckError("database operational query failed") from error

    try:
        await asyncio.to_thread(store.verify_writable)
    except Exception:
        pass
    else:
        status = replace(status, artifact_store_writable=True)

    print(render_status(status, settings.monitoring_window_hours))
    reasons = alert_reasons(status, settings)
    if reasons:
        raise OperationalCheckError("; ".join(reasons))
