import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config

from agent_eval.core.settings import Settings
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import (
    AgentStatus,
    ArtifactKind,
    AuthType,
    ResultValidityStatus,
    RunAgentStatus,
    RunStatus,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    ArtifactIndex,
    EvaluationRun,
    JudgeConfig,
    ResourceLease,
    RunAgent,
    ScoreConfig,
    WorkerLease,
)
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.maintenance.operational_check import alert_reasons, collect_operational_status

HEAD = "9d2a6f4c8b71"


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


async def test_operational_aggregates_use_safe_counts_and_database_time(tmp_path: Path) -> None:
    database_path = tmp_path / "monitor.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        data_dir=tmp_path,
        monitoring_resource_lease_grace_seconds=120,
    )
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    try:
        async with session_scope(sessions) as session:
            judge = JudgeConfig(provider="provider", model="model", prompt_version="v1")
            score = ScoreConfig(name="monitor-score")
            agent = Agent(name="Monitor Agent", provider="provider", status=AgentStatus.ENABLED)
            session.add_all([judge, score, agent])
            await session.flush()
            version = AgentVersion(
                agent_id=agent.id,
                version=1,
                endpoint_url="https://example.test/agent",
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
            )
            session.add(version)
            await session.flush()

            queued = EvaluationRun(
                name="not reported",
                status=RunStatus.QUEUED,
                dataset_bundle_hash="a" * 64,
                judge_config_id=judge.id,
                score_config_id=score.id,
                created_at=now - timedelta(minutes=40),
            )
            active = EvaluationRun(
                name="not reported",
                status=RunStatus.RUNNING,
                active_slot=1,
                claim_owner_id="secret-owner",
                claimed_at=now - timedelta(minutes=5),
                heartbeat_at=now - timedelta(minutes=5),
                dataset_bundle_hash="b" * 64,
                judge_config_id=judge.id,
                score_config_id=score.id,
            )
            published = EvaluationRun(
                name="not reported",
                status=RunStatus.PUBLISHED,
                finished_at=now,
                dataset_bundle_hash="c" * 64,
                judge_config_id=judge.id,
                score_config_id=score.id,
            )
            failed = EvaluationRun(
                name="not reported",
                status=RunStatus.SYSTEM_FAILED,
                finished_at=now,
                dataset_bundle_hash="d" * 64,
                judge_config_id=judge.id,
                score_config_id=score.id,
            )
            session.add_all([queued, active, published, failed])
            await session.flush()
            session.add_all(
                [
                    RunAgent(
                        run_id=published.id,
                        agent_version_id=version.id,
                        status=RunAgentStatus.COMPLETED,
                        validity_status=ResultValidityStatus.INVALID,
                    ),
                    RunAgent(
                        run_id=failed.id,
                        agent_version_id=version.id,
                        status=RunAgentStatus.COMPLETED,
                    ),
                    WorkerLease(
                        name="valid",
                        owner_id="secret-owner",
                        heartbeat_at=now,
                        lease_expires_at=now + timedelta(minutes=1),
                    ),
                    WorkerLease(
                        name="expired",
                        owner_id="secret-owner",
                        heartbeat_at=now - timedelta(minutes=5),
                        lease_expires_at=now - timedelta(minutes=1),
                    ),
                    ResourceLease(
                        resource_type="provider",
                        resource_key="secret-key",
                        slot_number=1,
                        holder_token="secret-token-1",
                        owner_id="secret-owner",
                        acquired_at=now,
                        lease_expires_at=now + timedelta(minutes=1),
                    ),
                    ResourceLease(
                        resource_type="provider",
                        resource_key="secret-key",
                        slot_number=2,
                        holder_token="secret-token-2",
                        owner_id="secret-owner",
                        acquired_at=now - timedelta(minutes=5),
                        lease_expires_at=now - timedelta(minutes=3),
                    ),
                    ArtifactIndex(
                        kind=ArtifactKind.AGENT_RESPONSE,
                        relative_path="secret/artifact-key",
                        sha256="e" * 64,
                        size_bytes=1,
                        expires_at=now + timedelta(days=1),
                    ),
                ]
            )

        async with sessions() as session:
            status = await collect_operational_status(session, settings, database_head=HEAD)
    finally:
        await engine.dispose()

    assert status.database_revision == HEAD
    assert status.queued_runs == 1
    assert status.oldest_queue_age_seconds >= 39 * 60
    assert status.active_runs == status.stale_active_runs == 1
    assert (status.valid_worker_leases, status.expired_worker_leases) == (1, 1)
    assert (status.active_resource_leases, status.expired_resource_leases) == (1, 1)
    assert status.recent_published_run_agents == 1
    assert status.recent_system_failed_run_agents == status.recent_system_failed_runs == 1
    assert status.recent_invalid_run_agents == status.artifact_index_count == 1
    assert (
        status.workspace_queued_attempts,
        status.workspace_oldest_queue_age_seconds,
        status.workspace_running_attempts,
        status.workspace_failed_attempts,
        status.workspace_recent_failed_attempts,
        status.workspace_stale_attempts,
    ) == (0, 0, 0, 0, 0, 0)


def test_operational_thresholds_exclude_invalid_results_by_default() -> None:
    from agent_eval.maintenance.operational_check import OperationalStatus

    status = OperationalStatus(
        database_revision=HEAD,
        database_head=HEAD,
        queued_runs=0,
        oldest_queue_age_seconds=0,
        active_runs=0,
        stale_active_runs=0,
        valid_worker_leases=1,
        expired_worker_leases=0,
        active_resource_leases=0,
        expired_resource_leases=0,
        recent_published_run_agents=0,
        recent_system_failed_run_agents=0,
        recent_system_failed_runs=0,
        recent_invalid_run_agents=99,
        artifact_index_count=0,
        artifact_store_writable=True,
    )
    settings = Settings()
    assert alert_reasons(status, settings) == []

    failed = replace(
        status,
        stale_active_runs=1,
        workspace_stale_attempts=1,
        workspace_queued_attempts=21,
        workspace_oldest_queue_age_seconds=1801,
        workspace_recent_failed_attempts=11,
        queued_runs=11,
        oldest_queue_age_seconds=1801,
        valid_worker_leases=0,
        expired_resource_leases=1,
        recent_system_failed_runs=1,
        artifact_store_writable=False,
    )
    assert len(alert_reasons(failed, settings)) == 11
