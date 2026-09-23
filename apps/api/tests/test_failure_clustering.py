import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_eval.api.main import create_app
from agent_eval.core.settings import Settings
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import (
    Ability,
    AgentStatus,
    AuthType,
    DatasetStatus,
    ExecutionStatus,
    Language,
    QuestionType,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    Benchmark,
    DatasetVersion,
    EvaluationRun,
    ExecutionItem,
    FailureCluster,
    JudgeConfig,
    Question,
    RunAgent,
    ScoreConfig,
    ScoreRecord,
)
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.quality.clustering import (
    LOW_SCORE_THRESHOLD,
    ClusteringError,
    collect_failure_samples,
    execute_failure_clustering,
)

OPERATOR_PASSWORD = "operator-secret"
password_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


@asynccontextmanager
async def clustering_database(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "failure-clustering.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        data_dir=tmp_path,
        environment="test",
    )
    engine = create_engine(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def seed_run_with_failures(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    failed: int,
    low_scored: int,
) -> tuple[UUID, list[UUID]]:
    async with session_scope(session_factory) as session:
        agent = Agent(name="Cluster Agent", provider="Test", status=AgentStatus.ENABLED)
        session.add(agent)
        await session.flush()
        version = AgentVersion(
            agent_id=agent.id,
            version=1,
            endpoint_url="https://agent.invalid",
            auth_type=AuthType.NONE,
            request_template='{"prompt":"{{prompt}}"}',
            answer_jsonpath="$.answer",
        )
        benchmark = Benchmark(
            name="Cluster Bench",
            ability=Ability.REASONING,
            language=Language.ZH,
            source="test",
        )
        judge = JudgeConfig(provider="test", model="judge", prompt_version="v1")
        score = ScoreConfig(name="Cluster Test")
        session.add_all([version, benchmark, judge, score])
        await session.flush()
        dataset = DatasetVersion(
            benchmark_id=benchmark.id,
            version=1,
            source_version="v1",
            content_hash="c" * 64,
            question_count=1,
            status=DatasetStatus.FROZEN,
            frozen_at=datetime.now(UTC),
        )
        session.add(dataset)
        await session.flush()
        question = Question(
            dataset_version_id=dataset.id,
            external_id="cluster-001",
            type=QuestionType.SHORT_ANSWER,
            prompt="证明某个命题",
            reference_answer="标准答案",
        )
        run = EvaluationRun(
            name="失败聚类回归",
            dataset_bundle_hash="d" * 64,
            judge_config_id=judge.id,
            score_config_id=score.id,
        )
        session.add_all([question, run])
        await session.flush()
        run_agent = RunAgent(
            run_id=run.id,
            agent_version_id=version.id,
            total=failed + low_scored,
            completed=low_scored,
            failed=failed,
        )
        session.add(run_agent)
        await session.flush()
        item_ids: list[UUID] = []
        items: list[ExecutionItem] = []
        for index in range(failed):
            item = ExecutionItem(
                run_agent_id=run_agent.id,
                question_id=question.id,
                trial_index=index,
                status=ExecutionStatus.FAILED,
                error_type="http_timeout",
            )
            session.add(item)
            items.append(item)
        for index in range(low_scored):
            item = ExecutionItem(
                run_agent_id=run_agent.id,
                question_id=question.id,
                trial_index=failed + index,
                status=ExecutionStatus.SCORED,
                answer_preview=f"答非所问的回答 {index}",
            )
            session.add(item)
            items.append(item)
        await session.flush()
        item_ids = [item.id for item in items]
        for item in items[failed:]:
            session.add(
                ScoreRecord(
                    execution_item_id=item.id,
                    answer_score=LOW_SCORE_THRESHOLD - 10,
                    final_score=LOW_SCORE_THRESHOLD - 10,
                    judge_reason="答非所问",
                )
            )
        return run.id, item_ids


def envelope(content: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
            "model": "deepseek-test",
        },
    )


def clustering_transport(
    calls: list[dict[str, object]], *, invalid_first: bool = False
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        user_message = json.loads(payload["messages"][1]["content"])
        failures = user_message["failures"]
        if "answer_preview" in failures[0]:
            if invalid_first and len(calls) == 1:
                return envelope({"unexpected": "shape"})
            return envelope(
                {
                    "summaries": [
                        {
                            "id": failure["id"],
                            "reason": (
                                "调用超时无响应"
                                if failure.get("error_type")
                                else "回答答非所问"
                            ),
                        }
                        for failure in failures
                    ]
                }
            )
        return envelope(
            {
                "clusters": [
                    {"name": "调用超时", "description": "Agent 接口超时，没有返回答案"},
                    {"name": "内容低质", "description": "回答与题目无关"},
                ],
                "assignments": [
                    {"id": failure["id"], "cluster": 0 if "超时" in failure["reason"] else 1}
                    for failure in failures
                ],
            }
        )

    return httpx.MockTransport(handler)


async def test_failure_clustering_persists_and_replaces_named_clusters(tmp_path: Path) -> None:
    async with clustering_database(tmp_path) as session_factory:
        run_id, item_ids = await seed_run_with_failures(
            session_factory, failed=3, low_scored=3
        )
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            data_dir=tmp_path,
            environment="test",
            deepseek_api_key=SecretStr("sk-test"),
        )
        calls: list[dict[str, object]] = []
        report = await execute_failure_clustering(
            session_factory,
            settings,
            run_id=run_id,
            transport=clustering_transport(calls),
        )

        assert report.skipped is None
        assert report.failure_count == 6
        assert report.cluster_count == 2
        assert report.llm_calls == 2
        assert len(calls) == 2

        async with session_factory() as session:
            clusters = list(
                await session.scalars(
                    select(FailureCluster).order_by(FailureCluster.name)
                )
            )
        assert [cluster.name for cluster in clusters] == ["内容低质", "调用超时"]
        for cluster in clusters:
            assert cluster.run_id == run_id
            assert cluster.window_days is None
            assert cluster.sample_count == 3
            assert cluster.failure_count == 6
            assert len(cluster.sample_item_ids) == 3
            assert set(cluster.sample_item_ids) <= {str(item_id) for item_id in item_ids}

        # 重跑同一 run：旧簇被替换而不是追加
        report = await execute_failure_clustering(
            session_factory,
            settings,
            run_id=run_id,
            transport=clustering_transport([]),
        )
        assert report.cluster_count == 2
        async with session_factory() as session:
            cluster_ids = list(await session.scalars(select(FailureCluster.id)))
        assert len(cluster_ids) == 2


async def test_failure_clustering_window_scope_collects_recent_runs(tmp_path: Path) -> None:
    async with clustering_database(tmp_path) as session_factory:
        await seed_run_with_failures(session_factory, failed=4, low_scored=2)
        async with session_factory() as session:
            samples = await collect_failure_samples(session, window_days=7)
        assert len(samples) == 6
        assert sum(sample.error_type == "http_timeout" for sample in samples) == 4
        assert sum(
            sample.final_score is not None and sample.final_score < LOW_SCORE_THRESHOLD
            for sample in samples
        ) == 2

        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            data_dir=tmp_path,
            environment="test",
            deepseek_api_key=SecretStr("sk-test"),
        )
        report = await execute_failure_clustering(
            session_factory,
            settings,
            window_days=7,
            transport=clustering_transport([]),
        )
        assert report.skipped is None
        assert report.cluster_count == 2
        async with session_factory() as session:
            cluster = await session.scalar(select(FailureCluster))
        assert cluster is not None
        assert cluster.run_id is None
        assert cluster.window_days == 7


async def test_failure_clustering_skips_when_failures_too_few(tmp_path: Path) -> None:
    async with clustering_database(tmp_path) as session_factory:
        run_id, _ = await seed_run_with_failures(session_factory, failed=2, low_scored=0)
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            data_dir=tmp_path,
            environment="test",
            deepseek_api_key=SecretStr("sk-test"),
        )

        def forbidden_handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("LLM must not be called for tiny failure sets")

        report = await execute_failure_clustering(
            session_factory,
            settings,
            run_id=run_id,
            transport=httpx.MockTransport(forbidden_handler),
        )
        assert report.skipped == "insufficient_failures"
        assert report.failure_count == 2
        assert report.llm_calls == 0
        async with session_factory() as session:
            assert await session.scalar(select(FailureCluster.id)) is None


async def test_failure_clustering_skips_without_deepseek_key(tmp_path: Path) -> None:
    async with clustering_database(tmp_path) as session_factory:
        run_id, _ = await seed_run_with_failures(session_factory, failed=6, low_scored=0)
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            data_dir=tmp_path,
            environment="test",
        )
        report = await execute_failure_clustering(session_factory, settings, run_id=run_id)
        assert report.skipped == "missing_deepseek_api_key"
        assert report.failure_count == 6


async def test_failure_clustering_retries_invalid_summary_response(tmp_path: Path) -> None:
    async with clustering_database(tmp_path) as session_factory:
        run_id, _ = await seed_run_with_failures(session_factory, failed=3, low_scored=3)
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            data_dir=tmp_path,
            environment="test",
            deepseek_api_key=SecretStr("sk-test"),
        )
        calls: list[dict[str, object]] = []
        report = await execute_failure_clustering(
            session_factory,
            settings,
            run_id=run_id,
            transport=clustering_transport(calls, invalid_first=True),
        )
        assert report.cluster_count == 2
        assert len(calls) == 3  # 摘要阶段重试一次 + 归并一次


async def test_failure_clustering_requires_exactly_one_scope(tmp_path: Path) -> None:
    async with clustering_database(tmp_path) as session_factory:
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            data_dir=tmp_path,
            environment="test",
        )
        with pytest.raises(ValueError, match="exactly one"):
            await execute_failure_clustering(session_factory, settings)
        with pytest.raises(ValueError, match="exactly one"):
            await execute_failure_clustering(
                session_factory, settings, run_id=uuid4(), window_days=7
            )
        with pytest.raises(ClusteringError, match="run_not_found"):
            await execute_failure_clustering(session_factory, settings, run_id=uuid4())


def test_failure_cluster_migration_creates_scoped_table(tmp_path: Path) -> None:
    database_path = tmp_path / "failure-cluster-migration.db"
    command.upgrade(migration_config(database_path), "head")

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info('failure_clusters')")
        }
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list('failure_clusters')")
        }

    assert {
        "run_id",
        "window_days",
        "name",
        "description",
        "sample_count",
        "failure_count",
        "sample_item_ids",
        "created_at",
    } <= columns
    assert "ix_failure_clusters_scope_created" in indexes


@asynccontextmanager
async def clustering_client(tmp_path: Path) -> AsyncIterator[tuple[AsyncClient, FastAPI]]:
    database_path = tmp_path / "failure-cluster-api.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        data_dir=tmp_path,
        environment="test",
        operator_password_hash=SecretStr(password_hasher.hash(OPERATOR_PASSWORD)),
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client, app


async def test_failure_clusters_api_lists_samples_with_run_links(tmp_path: Path) -> None:
    async with clustering_client(tmp_path) as (client, app):
        anonymous = await client.get("/api/v1/quality/clusters")
        assert anonymous.status_code in (401, 403)

        login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://test"},
            json={"role": "operator", "password": OPERATOR_PASSWORD},
        )
        assert login.status_code == 200

        sessions: async_sessionmaker[AsyncSession] = app.state.session_factory
        run_id, item_ids = await seed_run_with_failures(
            sessions, failed=3, low_scored=3
        )
        async with session_scope(sessions) as session:
            session.add(
                FailureCluster(
                    run_id=run_id,
                    name="调用超时",
                    description="Agent 接口超时，没有返回答案",
                    sample_count=3,
                    failure_count=6,
                    sample_item_ids=[str(item_id) for item_id in item_ids[:3]],
                )
            )
            session.add(
                FailureCluster(
                    window_days=7,
                    name="历史窗口簇",
                    description="最近 7 天窗口",
                    sample_count=2,
                    failure_count=8,
                    sample_item_ids=[str(item_id) for item_id in item_ids[3:5]],
                )
            )

        listed = await client.get("/api/v1/quality/clusters")
        assert listed.status_code == 200
        clusters = listed.json()
        assert len(clusters) == 2
        scoped = next(cluster for cluster in clusters if cluster["name"] == "调用超时")
        assert scoped["run_id"] == str(run_id)
        assert scoped["run_name"] == "失败聚类回归"
        assert scoped["window_days"] is None
        assert scoped["sample_count"] == 3
        assert scoped["failure_count"] == 6
        assert scoped["samples"] == [
            {"execution_item_id": str(item_id), "run_id": str(run_id)}
            for item_id in item_ids[:3]
        ]
        windowed = next(cluster for cluster in clusters if cluster["name"] == "历史窗口簇")
        assert windowed["run_id"] is None
        assert windowed["window_days"] == 7
        assert {sample["run_id"] for sample in windowed["samples"]} == {str(run_id)}

        filtered = await client.get(f"/api/v1/quality/clusters?run_id={run_id}")
        assert [cluster["name"] for cluster in filtered.json()] == ["调用超时"]
        empty = await client.get(f"/api/v1/quality/clusters?run_id={uuid4()}")
        assert empty.json() == []
