import asyncio
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from agent_eval.auth.identity import LEGACY_OPERATOR_USER_ID
from agent_eval.core.settings import Settings
from agent_eval.db.base import Base, utc_now
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import (
    Ability,
    ArtifactKind,
    AttemptStatus,
    AuthSource,
    AuthType,
    CitationFetchStatus,
    DatasetStatus,
    EvaluationCaseStatus,
    ExecutionStatus,
    Language,
    NotificationType,
    QuestionType,
    ResultValidityStatus,
    RunAgentStatus,
    RunStatus,
    SessionRole,
    WorkspaceAttemptStatus,
    WorkspaceMessageRole,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    ArtifactIndex,
    AuthUser,
    Benchmark,
    CitationRecord,
    DatasetVersion,
    EvaluationCase,
    EvaluationRun,
    ExecutionItem,
    HttpAttempt,
    JudgeConfig,
    LeaderboardResult,
    LeaderboardSnapshot,
    Notification,
    Question,
    ResourceLease,
    RunAgent,
    RunDatasetVersion,
    ScoreConfig,
    ScoreRecord,
    Session,
    WorkerLease,
    WorkspaceAttempt,
    WorkspaceComparison,
    WorkspaceConversation,
    WorkspaceMessage,
)
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.evaluation_cases.service import canonical_sha256
from agent_eval.maintenance import database_migration
from agent_eval.maintenance.database_migration import (
    ALEMBIC_HEAD,
    DatabaseMigrationError,
    fingerprint_database,
    migrate_database,
    migration_tables,
    validate_migration_urls,
    validate_sqlite_source,
)

POSTGRES_URL = os.getenv("AGENT_EVAL_TEST_POSTGRES_URL")


def migration_config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


async def create_sqlite_database(path: Path) -> str:
    url = f"sqlite+aiosqlite:///{path}"
    await asyncio.to_thread(command.upgrade, migration_config(url), "head")
    return url


def test_migration_urls_and_table_order_are_strict() -> None:
    validate_migration_urls(
        "sqlite+aiosqlite:////tmp/source.db",
        "postgresql+asyncpg://user:password@localhost/target",
    )
    with pytest.raises(DatabaseMigrationError, match="source database must use"):
        validate_migration_urls(
            "postgresql+asyncpg://localhost/source",
            "postgresql+asyncpg://localhost/target",
        )
    with pytest.raises(DatabaseMigrationError, match="target database must use"):
        validate_migration_urls(
            "sqlite+aiosqlite:////tmp/source.db",
            "postgresql+psycopg://localhost/target",
        )

    tables = migration_tables()
    assert "worker_leases" not in {table.name for table in tables}
    assert "resource_leases" not in {table.name for table in tables}
    positions = {table: index for index, table in enumerate(tables)}
    assert all(
        positions[foreign_key.column.table] < positions[table]
        for table in tables
        for foreign_key in table.foreign_keys
    )


async def test_sqlite_source_validation_rejects_wrong_head_active_runs_and_leases(
    tmp_path: Path,
) -> None:
    source_url = await create_sqlite_database(tmp_path / "source-validation.db")
    engine = create_engine(Settings(database_url=source_url))
    session_factory = create_session_factory(engine)
    try:
        async with engine.connect() as connection:
            await validate_sqlite_source(connection)

        async with engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num = 'old'"))
        async with engine.connect() as connection:
            with pytest.raises(DatabaseMigrationError, match="required Alembic head"):
                await validate_sqlite_source(connection)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :head"), {"head": ALEMBIC_HEAD}
            )

        async with session_scope(session_factory) as session:
            judge = JudgeConfig(provider="judge", model="model", prompt_version="v1")
            score = ScoreConfig(name="source validation")
            session.add_all([judge, score])
            await session.flush()
            session.add(
                EvaluationRun(
                    name="active",
                    status=RunStatus.RUNNING,
                    active_slot=1,
                    claim_owner_id="worker",
                    claimed_at=utc_now(),
                    dataset_bundle_hash="a" * 64,
                    judge_config_id=judge.id,
                    score_config_id=score.id,
                )
            )
        async with engine.connect() as connection:
            with pytest.raises(DatabaseMigrationError, match="active evaluation runs"):
                await validate_sqlite_source(connection)

        async with session_scope(session_factory) as session:
            run = await session.scalar(select(EvaluationRun))
            assert run is not None
            run.status = RunStatus.CANCELED
            run.active_slot = None
            run.claim_owner_id = None
            run.claimed_at = None
            session.add(
                WorkerLease(
                    name="stopped-worker",
                    owner_id="stopped-worker",
                    lease_expires_at=utc_now() - timedelta(hours=1),
                    heartbeat_at=utc_now() - timedelta(hours=2),
                )
            )
        async with engine.connect() as connection:
            with pytest.raises(DatabaseMigrationError, match="worker leases"):
                await validate_sqlite_source(connection)
    finally:
        await engine.dispose()


async def test_sqlite_source_ignores_expired_resource_leases_but_rejects_active(
    tmp_path: Path,
) -> None:
    source_url = await create_sqlite_database(tmp_path / "resource-source-validation.db")
    engine = create_engine(Settings(database_url=source_url))
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            session.add(
                ResourceLease(
                    resource_type="request",
                    resource_key="agent",
                    slot_number=1,
                    holder_token="b" * 32,
                    owner_id="stopped-worker",
                    acquired_at=utc_now() - timedelta(hours=2),
                    lease_expires_at=utc_now() - timedelta(hours=1),
                )
            )
        async with engine.connect() as connection:
            await validate_sqlite_source(connection)

        async with session_scope(session_factory) as session:
            lease = await session.get(ResourceLease, "b" * 32)
            assert lease is not None
            lease.lease_expires_at = utc_now() + timedelta(hours=1)
        async with engine.connect() as connection:
            with pytest.raises(DatabaseMigrationError, match="active resource leases"):
                await validate_sqlite_source(connection)
    finally:
        await engine.dispose()


async def test_sqlite_source_rejects_active_workspace_attempts(tmp_path: Path) -> None:
    source_url = await create_sqlite_database(tmp_path / "workspace-source-validation.db")
    engine = create_engine(Settings(database_url=source_url))
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            agent = Agent(name="Workspace migration agent", provider="provider")
            session.add(agent)
            await session.flush()
            version = AgentVersion(
                agent_id=agent.id,
                version=1,
                endpoint_url="https://agent.invalid/workspace",
                auth_type=AuthType.NONE,
                request_template='{"prompt":"{{prompt}}"}',
                answer_jsonpath="$.answer",
            )
            session.add(version)
            await session.flush()
            conversation = WorkspaceConversation(
                owner_user_id=LEGACY_OPERATOR_USER_ID,
                pinned_agent_version_id=version.id,
                title="Active workspace migration",
                last_turn_no=1,
                revision=1,
            )
            session.add(conversation)
            await session.flush()
            message = WorkspaceMessage(
                conversation_id=conversation.id,
                turn_no=1,
                role=WorkspaceMessageRole.USER,
                variant_no=1,
                active=True,
                content="Pending",
            )
            session.add(message)
            await session.flush()
            attempt = WorkspaceAttempt(
                conversation_id=conversation.id,
                user_message_id=message.id,
                agent_version_id=version.id,
                generation_no=1,
                idempotency_key="00000000-0000-4000-8000-000000000001",
                request_fingerprint="a" * 64,
                status=WorkspaceAttemptStatus.QUEUED,
                prompt_hash="b" * 64,
                prompt_characters=100,
                prompt_format="workspace-transcript-v1",
            )
            session.add(attempt)
            await session.flush()
            attempt_id = attempt.id

        async with engine.connect() as connection:
            with pytest.raises(DatabaseMigrationError, match="active workspace attempts"):
                await validate_sqlite_source(connection)

        async with session_scope(session_factory) as session:
            attempt = await session.get(WorkspaceAttempt, attempt_id)
            assert attempt is not None
            attempt.status = WorkspaceAttemptStatus.CANCELED
            attempt.finished_at = utc_now()
            attempt.error_type = "workspace_canceled"
            attempt.error_category = "canceled"
        async with engine.connect() as connection:
            await validate_sqlite_source(connection)
    finally:
        await engine.dispose()


async def seed_representative_source(source_url: str) -> None:
    engine = create_engine(Settings(database_url=source_url))
    session_factory = create_session_factory(engine)
    now = utc_now().replace(microsecond=123456)
    try:
        async with session_scope(session_factory) as session:
            workspace_user = AuthUser(
                subject_key="local:migration.operator",
                username="migration.operator",
                display_name="Migration Operator",
                role=SessionRole.OPERATOR,
                auth_source=AuthSource.LOCAL,
                password_hash="$argon2id$migration-fixture",
                is_shared=False,
                workspace_enabled=True,
            )
            agent = Agent(name="Migration Agent", provider="Provider", description="representative")
            benchmark = Benchmark(
                name="Migration Benchmark",
                ability=Ability.RETRIEVAL,
                language=Language.EN,
                source="internal",
            )
            judge = JudgeConfig(provider="judge", model="model", prompt_version="v1")
            score_config = ScoreConfig(name="Migration Score")
            artifact = ArtifactIndex(
                kind=ArtifactKind.AGENT_RESPONSE,
                relative_path="runs/migration/response.json",
                sha256="1" * 64,
                size_bytes=321,
                expires_at=now + timedelta(days=30),
            )
            judge_artifact = ArtifactIndex(
                kind=ArtifactKind.JUDGE_RESPONSE,
                relative_path="runs/migration/judge.json",
                sha256="2" * 64,
                size_bytes=654,
                expires_at=now + timedelta(days=30),
            )
            session.add_all(
                [
                    workspace_user,
                    agent,
                    benchmark,
                    judge,
                    score_config,
                    artifact,
                    judge_artifact,
                ]
            )
            await session.flush()

            agent_version = AgentVersion(
                agent_id=agent.id,
                version=1,
                endpoint_url="https://agent.invalid/v1",
                auth_type=AuthType.BEARER,
                encrypted_token="gAAAAAB-encrypted-looking-token-material",
                request_template='{"prompt":"{{prompt}}","metadata":{"mode":"full"}}',
                answer_jsonpath="$.answer",
                citations_jsonpath="$.citations",
                preflight_report_json=[{"check": "schema", "passed": True}],
                preflight_at=now,
            )
            dataset = DatasetVersion(
                benchmark_id=benchmark.id,
                version=1,
                source_version="source-v1",
                sampling_seed=42,
                content_hash="3" * 64,
                question_count=1,
                cohort_key="legacy-v1",
                status=DatasetStatus.FROZEN,
                frozen_at=now,
            )
            session.add_all([agent_version, dataset])
            await session.flush()

            comparison = WorkspaceComparison(
                owner_user_id=workspace_user.id,
                title="Migrated comparison",
                prompt="Migration question",
                idempotency_key="00000000-0000-4000-8000-000000000003",
                request_fingerprint="a" * 64,
            )
            session.add(comparison)
            await session.flush()
            conversation = WorkspaceConversation(
                owner_user_id=workspace_user.id,
                pinned_agent_version_id=agent_version.id,
                title="Migrated workspace history",
                last_turn_no=1,
                revision=1,
                comparison_id=comparison.id,
                comparison_position=1,
            )
            session.add(conversation)
            await session.flush()
            workspace_user_message = WorkspaceMessage(
                conversation_id=conversation.id,
                turn_no=1,
                role=WorkspaceMessageRole.USER,
                variant_no=1,
                active=True,
                content="Migration question",
            )
            workspace_assistant_message = WorkspaceMessage(
                conversation_id=conversation.id,
                turn_no=1,
                role=WorkspaceMessageRole.ASSISTANT,
                variant_no=1,
                active=True,
                content="Migration answer",
                citations=["https://example.invalid/workspace-source"],
            )
            session.add_all([workspace_user_message, workspace_assistant_message])
            await session.flush()
            workspace_attempt = WorkspaceAttempt(
                conversation_id=conversation.id,
                user_message_id=workspace_user_message.id,
                assistant_message_id=workspace_assistant_message.id,
                agent_version_id=agent_version.id,
                generation_no=1,
                idempotency_key="00000000-0000-4000-8000-000000000002",
                request_fingerprint="8" * 64,
                status=WorkspaceAttemptStatus.SUCCEEDED,
                claimed_at=now - timedelta(seconds=2),
                started_at=now - timedelta(seconds=2),
                finished_at=now - timedelta(seconds=1),
                http_attempt_count=1,
                last_http_status=200,
                latency_ms=125,
                input_tokens=10,
                output_tokens=2,
                total_tokens=12,
                artifact_id=artifact.id,
                prompt_hash="9" * 64,
                prompt_characters=500,
                prompt_format="workspace-compare-v1",
            )
            session.add(workspace_attempt)
            await session.flush()
            source_snapshot = {
                "agent": {
                    "agent_id": str(agent.id),
                    "agent_version_id": str(agent_version.id),
                    "name": agent.name,
                    "provider": agent.provider,
                    "version": agent_version.version,
                },
                "artifact_sha256": artifact.sha256,
                "assistant_message_id": str(workspace_assistant_message.id),
                "attempt_id": str(workspace_attempt.id),
                "candidate_answer": workspace_assistant_message.content,
                "citations": list(workspace_assistant_message.citations or []),
                "comparison_id": str(comparison.id),
                "conversation_id": str(conversation.id),
                "generation_no": workspace_attempt.generation_no,
                "input_tokens": workspace_attempt.input_tokens,
                "latency_ms": workspace_attempt.latency_ms,
                "output_tokens": workspace_attempt.output_tokens,
                "prompt_format": workspace_attempt.prompt_format,
                "prompt_hash": workspace_attempt.prompt_hash,
                "total_tokens": workspace_attempt.total_tokens,
                "user_message": workspace_user_message.content,
                "user_message_id": str(workspace_user_message.id),
            }
            session.add(
                EvaluationCase(
                    owner_user_id=workspace_user.id,
                    source_attempt_id=workspace_attempt.id,
                    source_conversation_id=conversation.id,
                    idempotency_key="00000000-0000-4000-8000-000000000004",
                    request_fingerprint="b" * 64,
                    source_hash=canonical_sha256(source_snapshot),
                    source_payload_json=source_snapshot,
                    status=EvaluationCaseStatus.DRAFT,
                    revision=0,
                    prompt="Migration question",
                )
            )

            question = Question(
                dataset_version_id=dataset.id,
                external_id="migration-question",
                type=QuestionType.MULTIPLE_CHOICE,
                prompt="Which value is correct?",
                choices_json=[{"label": "A", "text": "one"}, {"label": "B", "text": "two"}],
                reference_answer="A",
                requires_citations=True,
                difficulty="medium",
                domain="migration",
            )
            run = EvaluationRun(
                name="Published migration run",
                status=RunStatus.PUBLISHED,
                dataset_bundle_hash="4" * 64,
                protocol_hash="5" * 64,
                judge_config_id=judge.id,
                score_config_id=score_config.id,
                started_at=now - timedelta(minutes=2),
                finished_at=now,
            )
            session.add_all([question, run])
            await session.flush()

            run_agent = RunAgent(
                run_id=run.id,
                agent_version_id=agent_version.id,
                status=RunAgentStatus.COMPLETED,
                total=1,
                completed=1,
                validity_status=ResultValidityStatus.VALID,
                configuration_failure_count=0,
                configuration_failure_rate=Decimal("0.0000"),
            )
            session.add_all(
                [run_agent, RunDatasetVersion(run_id=run.id, dataset_version_id=dataset.id)]
            )
            await session.flush()

            item = ExecutionItem(
                run_agent_id=run_agent.id,
                question_id=question.id,
                status=ExecutionStatus.SCORED,
                answer_preview="A",
                answer_hash="6" * 64,
                attempt_count=1,
                latency_ms=125,
                artifact_id=artifact.id,
            )
            session.add(item)
            await session.flush()
            session.add_all(
                [
                    HttpAttempt(
                        execution_item_id=item.id,
                        attempt_no=1,
                        status=AttemptStatus.SUCCEEDED,
                        started_at=now - timedelta(seconds=1),
                        finished_at=now,
                        status_code=200,
                        latency_ms=125,
                        input_tokens=10,
                        output_tokens=2,
                        total_tokens=12,
                    ),
                    CitationRecord(
                        execution_item_id=item.id,
                        position=0,
                        original_url="https://example.invalid/source",
                        final_url="https://example.invalid/final",
                        fetch_status=CitationFetchStatus.FETCHED,
                        http_status=200,
                        authenticity_score=Decimal("91.25"),
                        support_score=Decimal("87.50"),
                        judge_reason="supported",
                        artifact_id=artifact.id,
                        judge_artifact_id=judge_artifact.id,
                    ),
                    ScoreRecord(
                        execution_item_id=item.id,
                        answer_score=Decimal("92.50"),
                        citation_score=Decimal("89.25"),
                        final_score=Decimal("91.20"),
                        judge_reason="representative",
                        judge_artifact_id=judge_artifact.id,
                    ),
                ]
            )
            snapshot = LeaderboardSnapshot(
                run_id=run.id,
                published_at=now,
                rankings_json=[{"agent_id": str(agent.id), "score": 91.2}],
                is_current=True,
            )
            session.add(snapshot)
            await session.flush()
            session.add_all(
                [
                    LeaderboardResult(
                        snapshot_id=snapshot.id,
                        run_agent_id=run_agent.id,
                        agent_id=agent.id,
                        agent_version_id=agent_version.id,
                        metric="overall",
                        score=Decimal("91.200000000000000000"),
                        protocol_hash="7" * 64,
                        benchmark_count=1,
                    ),
                    Session(
                        user_id=workspace_user.id,
                        role=SessionRole.OPERATOR,
                        token_hash="8" * 64,
                        csrf_token_hash="9" * 64,
                        expires_at=now + timedelta(hours=8),
                        last_seen_at=now,
                        created_at=now - timedelta(minutes=5),
                    ),
                    Notification(
                        type=NotificationType.RUN_PUBLISHED,
                        title="Migration publication",
                        message="Representative notification state",
                        created_at=now,
                    ),
                    ResourceLease(
                        resource_type="request",
                        resource_key="expired-before-cutover",
                        slot_number=1,
                        holder_token="a" * 32,
                        owner_id="stopped-worker",
                        acquired_at=now - timedelta(hours=2),
                        lease_expires_at=now - timedelta(hours=1),
                    ),
                ]
            )
    finally:
        await engine.dispose()


async def assert_target_empty(target_url: str) -> None:
    engine = create_engine(Settings(database_url=target_url))
    try:
        async with engine.connect() as connection:
            for table in Base.metadata.sorted_tables:
                expected = 2 if table.name == "auth_users" else 0
                assert await connection.scalar(select(func.count()).select_from(table)) == expected
    finally:
        await engine.dispose()


@pytest.mark.postgresql
@pytest.mark.skipif(not POSTGRES_URL, reason="AGENT_EVAL_TEST_POSTGRES_URL is not set")
async def test_sqlite_to_postgresql_migration_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert POSTGRES_URL is not None
    source_url = await create_sqlite_database(tmp_path / "migration-source.db")
    await seed_representative_source(source_url)
    target_config = migration_config(POSTGRES_URL)
    await asyncio.to_thread(command.downgrade, target_config, "base")
    await asyncio.to_thread(command.upgrade, target_config, "head")

    try:
        dry_run_report = await migrate_database(
            source_url=source_url,
            target_url=POSTGRES_URL,
            batch_size=2,
            dry_run=True,
        )
        assert dry_run_report.dry_run
        assert dry_run_report.total_rows > 0
        await assert_target_empty(POSTGRES_URL)

        original_copy_table = database_migration._copy_table
        copy_calls = 0

        async def fail_during_copy(*args: object, **kwargs: object) -> None:
            nonlocal copy_calls
            await original_copy_table(*args, **kwargs)  # type: ignore[arg-type]
            copy_calls += 1
            if copy_calls == 2:
                raise RuntimeError("injected copy failure")

        monkeypatch.setattr(database_migration, "_copy_table", fail_during_copy)
        with pytest.raises(DatabaseMigrationError, match="rolled back"):
            await migrate_database(
                source_url=source_url,
                target_url=POSTGRES_URL,
                batch_size=1,
            )
        await assert_target_empty(POSTGRES_URL)
        monkeypatch.setattr(database_migration, "_copy_table", original_copy_table)

        report = await migrate_database(
            source_url=source_url,
            target_url=POSTGRES_URL,
            batch_size=2,
        )
        assert not report.dry_run
        assert report.total_rows == dry_run_report.total_rows
        assert report.tables == dry_run_report.tables

        source_engine = create_engine(Settings(database_url=source_url))
        target_engine = create_engine(Settings(database_url=POSTGRES_URL))
        try:
            async with source_engine.connect() as source, target_engine.connect() as target:
                source_fingerprints = await fingerprint_database(source, batch_size=2)
                target_fingerprints = await fingerprint_database(target, batch_size=2)
                assert source_fingerprints == target_fingerprints
        finally:
            await source_engine.dispose()
            await target_engine.dispose()

        with pytest.raises(DatabaseMigrationError, match="not empty"):
            await migrate_database(
                source_url=source_url,
                target_url=POSTGRES_URL,
                batch_size=2,
            )
    finally:
        await asyncio.to_thread(command.downgrade, target_config, "base")
