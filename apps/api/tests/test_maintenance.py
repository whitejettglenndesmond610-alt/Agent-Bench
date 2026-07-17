import asyncio
import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from agent_eval.auth.security import verify_password
from agent_eval.core.artifact_store import LocalArtifactStore
from agent_eval.core.settings import Settings
from agent_eval.core.storage import ensure_storage
from agent_eval.db.base import utc_now
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import (
    AgentStatus,
    ArtifactKind,
    ResultValidityStatus,
    RunAgentStatus,
    RunScope,
    RunStatus,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    ArtifactIndex,
    EvaluationRun,
    ExecutionItem,
    JudgeConfig,
    LeaderboardResult,
    LeaderboardSnapshot,
    RunAgent,
    ScoreConfig,
)
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.demo.fake_services import fake_answer, judge
from agent_eval.maintenance.bootstrap import (
    bootstrap_demo,
    demo_preflight,
    validate_dataset_manifest,
)
from agent_eval.maintenance.demo import queue_fake_run, queue_real_run, seed_fake_agents
from agent_eval.maintenance.operations import (
    archive_current_leaderboard,
    cleanup_expired_artifacts,
    create_backup,
    restore_latest_backup,
    revalidate_ranking_results,
    verify_latest_backup,
)
from agent_eval.maintenance.secrets import (
    initialize_platform_secrets,
    store_runtime_secret,
    write_demo_environment,
    write_local_real_environment,
)


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


async def migrated_settings(tmp_path: Path) -> Settings:
    database_path = tmp_path / "maintenance.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        data_dir=tmp_path,
    )
    ensure_storage(settings)
    return settings


def test_manifest_validation_detects_content_changes(tmp_path: Path) -> None:
    dataset_file = tmp_path / "questions.jsonl"
    dataset_file.write_text("{}\n")
    manifest = {
        "question_count": 100,
        "benchmarks": {
            "Demo": {
                "file": dataset_file.name,
                "file_sha256": hashlib.sha256(dataset_file.read_bytes()).hexdigest(),
            }
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    validate_dataset_manifest(tmp_path)

    dataset_file.write_text('{"changed":true}\n')
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_dataset_manifest(tmp_path)


async def test_bootstrap_is_idempotent_and_preflight_reports_runtime_gaps(
    tmp_path: Path,
) -> None:
    settings = await migrated_settings(tmp_path)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            first = await bootstrap_demo(session, Path("data/demo-dataset"))
        async with session_scope(session_factory) as session:
            second = await bootstrap_demo(session, Path("data/demo-dataset"))
        assert first.question_count == 100
        assert first.dataset_count == 6
        assert first.judge_created is True
        assert first.score_created is True
        assert second.judge_created is False
        assert second.score_created is False

        async with session_factory() as session:
            failures = await demo_preflight(session, settings, require_snapshot=True)
            judge_count = await session.scalar(select(func.count()).select_from(JudgeConfig))
            score_count = await session.scalar(select(func.count()).select_from(ScoreConfig))
        assert judge_count == 1
        assert score_count == 2
        assert any("2 enabled Agent" in failure for failure in failures)
        assert any("DeepSeek API key" in failure for failure in failures)
        assert any("fallback leaderboard" in failure for failure in failures)

        async with session_scope(session_factory) as session:
            assert len(await seed_fake_agents(session)) == 2
            run_id = await queue_fake_run(session, question_count=20)
            comprehensive_run_id = await queue_fake_run(session, question_count=100)
        ready_settings = Settings(
            database_url=settings.database_url,
            data_dir=tmp_path,
            deepseek_api_key="local-fake-key",
        )
        async with session_factory() as session:
            smoke_run = await session.get(EvaluationRun, run_id)
            comprehensive_run = await session.get(EvaluationRun, comprehensive_run_id)
            post_demo_failures = await demo_preflight(
                session, ready_settings, require_snapshot=False
            )
            assert len(post_demo_failures) == 1
            assert "enabled Agent versions" in post_demo_failures[0]
            item_count = await session.scalar(
                select(func.count())
                .select_from(ExecutionItem)
                .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                .where(RunAgent.run_id == run_id)
            )
        assert smoke_run is not None
        assert smoke_run.profile_key == "smoke"
        assert smoke_run.run_scope == RunScope.SMOKE
        assert smoke_run.ranking_eligible is False
        assert comprehensive_run is not None
        assert comprehensive_run.profile_key == "comprehensive"
        assert comprehensive_run.run_scope == RunScope.COMPREHENSIVE
        assert comprehensive_run.ranking_eligible is False
        assert item_count == 40

        async with session_scope(session_factory) as session:
            agents = list(
                await session.scalars(
                    select(Agent).where(Agent.name.like("Fake Agent %"))
                )
            )
            assert len(agents) == 2
            agents[0].name = "Qwen Plus"
            agents[1].name = "Qwen Max"
            agents[0].status = AgentStatus.ENABLED
            agents[1].status = AgentStatus.ENABLED
        async with session_scope(session_factory) as session:
            real_run_id = await queue_real_run(session, question_count=20)
        async with session_factory() as session:
            real_item_count = await session.scalar(
                select(func.count())
                .select_from(ExecutionItem)
                .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                .where(RunAgent.run_id == real_run_id)
            )
        assert real_item_count == 40
    finally:
        await engine.dispose()


async def test_online_backup_verification_and_restore(tmp_path: Path) -> None:
    settings = await migrated_settings(tmp_path)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            session.add(ScoreConfig(name="Before backup"))
    finally:
        await engine.dispose()

    manifest = create_backup(settings)
    assert verify_latest_backup(settings) == manifest

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            session.add(ScoreConfig(name="After backup"))
    finally:
        await engine.dispose()

    with pytest.raises(RuntimeError, match="confirm-stopped"):
        restore_latest_backup(settings, confirm_stopped=False)
    restore_latest_backup(settings, confirm_stopped=True)

    database_path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    with sqlite3.connect(database_path) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM score_configs")}
    assert names == {"Before backup"}
    assert list(settings.backup_dir.glob("agent-eval-*.db")) == [
        settings.backup_dir / manifest.filename
    ]


async def test_cleanup_removes_only_expired_indexed_artifacts(tmp_path: Path) -> None:
    settings = await migrated_settings(tmp_path)
    expired_path = settings.artifact_dir / "runs" / "expired.json"
    current_path = settings.artifact_dir / "runs" / "current.json"
    expired_path.parent.mkdir(parents=True)
    expired_path.write_bytes(b"expired")
    current_path.write_bytes(b"current")

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            session.add_all(
                [
                    ArtifactIndex(
                        kind=ArtifactKind.AGENT_RESPONSE,
                        relative_path="runs/expired.json",
                        sha256="a" * 64,
                        size_bytes=7,
                        expires_at=utc_now() - timedelta(seconds=1),
                    ),
                    ArtifactIndex(
                        kind=ArtifactKind.AGENT_RESPONSE,
                        relative_path="runs/current.json",
                        sha256="b" * 64,
                        size_bytes=7,
                        expires_at=utc_now() + timedelta(days=1),
                    ),
                ]
            )
        async with session_scope(session_factory) as session:
            assert await cleanup_expired_artifacts(
                session, LocalArtifactStore(settings.artifact_dir)
            ) == (1, 1)
        async with session_factory() as session:
            remaining = await session.scalar(select(func.count()).select_from(ArtifactIndex))
    finally:
        await engine.dispose()

    assert remaining == 1
    assert not expired_path.exists()
    assert current_path.read_bytes() == b"current"


async def test_cleanup_keeps_failed_row_without_rolling_back_prior_deletes(tmp_path: Path) -> None:
    settings = await migrated_settings(tmp_path)

    class FailingStore(LocalArtifactStore):
        def delete(self, key: str) -> bool:
            if key == "runs/second.json":
                raise OSError("storage unavailable")
            return super().delete(key)

    store = FailingStore(settings.artifact_dir)
    first = store.put("runs/first.json", b"first")
    second = store.put("runs/second.json", b"second")
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            session.add_all(
                [
                    ArtifactIndex(
                        id=UUID(int=1),
                        kind=ArtifactKind.AGENT_RESPONSE,
                        relative_path=first.relative_path,
                        sha256=first.sha256,
                        size_bytes=first.size_bytes,
                        expires_at=utc_now() - timedelta(seconds=1),
                    ),
                    ArtifactIndex(
                        id=UUID(int=2),
                        kind=ArtifactKind.AGENT_RESPONSE,
                        relative_path=second.relative_path,
                        sha256=second.sha256,
                        size_bytes=second.size_bytes,
                        expires_at=utc_now() - timedelta(seconds=1),
                    ),
                ]
            )
        async with session_factory() as session:
            with pytest.raises(OSError, match="storage unavailable"):
                await cleanup_expired_artifacts(session, store)
            await session.rollback()
        async with session_factory() as session:
            remaining = list(await session.scalars(select(ArtifactIndex.relative_path)))
    finally:
        await engine.dispose()

    assert remaining == ["runs/second.json"]
    assert not (settings.artifact_dir / first.relative_path).exists()
    assert (settings.artifact_dir / second.relative_path).exists()


async def test_archive_current_leaderboard_is_idempotent_and_preserves_history(
    tmp_path: Path,
) -> None:
    settings = await migrated_settings(tmp_path)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            judge = JudgeConfig(provider="judge", model="model", prompt_version="v1")
            score = ScoreConfig(name="Score")
            session.add_all([judge, score])
            await session.flush()
            run = EvaluationRun(
                name="Historical leaderboard",
                dataset_bundle_hash="a" * 64,
                profile_key="comprehensive",
                judge_config_id=judge.id,
                score_config_id=score.id,
            )
            session.add(run)
            await session.flush()
            snapshot = LeaderboardSnapshot(
                run_id=run.id,
                rankings_json=[],
                is_current=True,
            )
            session.add(snapshot)
            await session.flush()
            snapshot_id = snapshot.id

        async with session_scope(session_factory) as session:
            assert await archive_current_leaderboard(session) == 1
        async with session_scope(session_factory) as session:
            assert await archive_current_leaderboard(session) == 0
        async with session_factory() as session:
            snapshots = list(await session.scalars(select(LeaderboardSnapshot)))
        assert len(snapshots) == 1
        assert snapshots[0].id == snapshot_id
        assert snapshots[0].is_current is False
    finally:
        await engine.dispose()


async def test_revalidate_ranking_results_cleans_invalid_rows_and_repairs_current(
    tmp_path: Path,
) -> None:
    settings = await migrated_settings(tmp_path)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            await bootstrap_demo(session, Path("data/demo-dataset"))
            await seed_fake_agents(session)
            older_run_id = await queue_fake_run(session, question_count=20)
            newer_run_id = await queue_fake_run(session, question_count=20)

        async with session_scope(session_factory) as session:
            versions = {
                version.id: version for version in await session.scalars(select(AgentVersion))
            }
            run_agents_by_run: dict[object, list[RunAgent]] = {}
            for run_id in (older_run_id, newer_run_id):
                run = await session.get(EvaluationRun, run_id)
                assert run is not None
                run.status = RunStatus.PUBLISHED
                run.profile_key = "unified-v3"
                run.run_scope = RunScope.COMPREHENSIVE
                run.ranking_eligible = True
                run_agents = list(
                    await session.scalars(select(RunAgent).where(RunAgent.run_id == run_id))
                )
                run_agents_by_run[run_id] = run_agents
                for run_agent in run_agents:
                    run_agent.status = RunAgentStatus.COMPLETED
                    items = list(
                        await session.scalars(
                            select(ExecutionItem)
                            .where(ExecutionItem.run_agent_id == run_agent.id)
                            .order_by(ExecutionItem.id)
                        )
                    )
                    if run_id == newer_run_id:
                        for item in items[:4]:
                            item.error_type = "agent_client_error"

                snapshot = LeaderboardSnapshot(
                    run_id=run_id,
                    rankings_json=[],
                    is_current=run_id == newer_run_id,
                )
                session.add(snapshot)
                await session.flush()
                for run_agent in run_agents:
                    version = versions[run_agent.agent_version_id]
                    session.add(
                        LeaderboardResult(
                            snapshot_id=snapshot.id,
                            run_agent_id=run_agent.id,
                            agent_id=version.agent_id,
                            agent_version_id=version.id,
                            metric="overall",
                            score=Decimal("80"),
                            protocol_hash="a" * 64,
                            benchmark_count=1,
                        )
                    )

        async with session_scope(session_factory) as session:
            first = await revalidate_ranking_results(session)
        assert first.run_count == 2
        assert first.run_agent_count == 4
        assert first.valid_count == 2
        assert first.invalid_count == 2
        assert first.updated_count == 4
        assert first.deleted_result_count == 2
        assert first.current_repaired_count == 1

        async with session_scope(session_factory) as session:
            second = await revalidate_ranking_results(session)
        assert second.updated_count == 0
        assert second.deleted_result_count == 0
        assert second.current_repaired_count == 0

        async with session_factory() as session:
            snapshots = list(await session.scalars(select(LeaderboardSnapshot)))
            remaining_results = list(await session.scalars(select(LeaderboardResult)))
            run_agents = list(await session.scalars(select(RunAgent)))
        assert len(snapshots) == 2
        assert next(
            snapshot for snapshot in snapshots if snapshot.run_id == older_run_id
        ).is_current
        assert not next(
            snapshot for snapshot in snapshots if snapshot.run_id == newer_run_id
        ).is_current
        assert {result.run_agent_id for result in remaining_results} == {
            run_agent.id for run_agent in run_agents_by_run[older_run_id]
        }
        assert {
            run_agent.validity_status
            for run_agent in run_agents
            if run_agent.run_id == newer_run_id
        } == {ResultValidityStatus.INVALID}
    finally:
        await engine.dispose()


def test_secret_initialization_is_private_and_never_overwrites(tmp_path: Path) -> None:
    secret_dir = tmp_path / "secrets"

    initialized = initialize_platform_secrets(secret_dir)

    credentials = dict(
        line.split("=", maxsplit=1)
        for line in initialized.credentials_file.read_text().splitlines()
    )
    read_only_hash = (secret_dir / "read-only-password-hash").read_text().strip()
    operator_hash = (secret_dir / "operator-password-hash").read_text().strip()
    assert credentials["read_only_password"] != credentials["operator_password"]
    assert verify_password(credentials["read_only_password"], read_only_hash)
    assert verify_password(credentials["operator_password"], operator_hash)
    assert secret_dir.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in secret_dir.iterdir())
    assert initialized.credentials_file.parent == secret_dir.parent
    assert initialized.credentials_file.stat().st_mode & 0o777 == 0o600

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        initialize_platform_secrets(secret_dir)

    demo_env = write_demo_environment(secret_dir, tmp_path / "demo.env")
    demo_env_content = demo_env.read_text()
    assert credentials["read_only_password"] not in demo_env_content
    assert credentials["operator_password"] not in demo_env_content
    assert demo_env.stat().st_mode & 0o777 == 0o600

    key_path = store_runtime_secret(secret_dir, "deepseek-api-key", "deepseek-secret")
    assert key_path.stat().st_mode & 0o777 == 0o600
    local_env = write_local_real_environment(secret_dir, tmp_path / "local-real.env")
    local_env_content = local_env.read_text()
    assert "deepseek-secret" in local_env_content
    assert credentials["operator_password"] not in local_env_content
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        store_runtime_secret(secret_dir, "deepseek-api-key", "replacement")


async def test_fake_services_support_two_agent_shapes_and_judge_schemas() -> None:
    assert fake_answer("只返回一个选项标识", "alpha") == "A"
    assert fake_answer("只返回一个选项标识", "beta") == "B"

    short = await judge({"messages": [{"content": "verdict"}]})
    opened = await judge({"messages": [{"content": "rating"}]})
    citation = await judge({"messages": [{"content": "support_score"}]})
    assert "verdict" in short["choices"][0]["message"]["content"]
    assert "rating" in opened["choices"][0]["message"]["content"]
    assert "support_score" in citation["choices"][0]["message"]["content"]
