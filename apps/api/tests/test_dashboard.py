import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_eval.api.main import create_app
from agent_eval.core.settings import Settings
from agent_eval.db.enums import Ability, DatasetStatus, Language
from agent_eval.db.models import Benchmark, DatasetVersion, JudgeConfig, ScoreConfig
from agent_eval.db.session import session_scope

READ_ONLY_PASSWORD = "readonly-secret"
OPERATOR_PASSWORD = "operator-secret"
password_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


async def seed_dashboard(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(session_factory) as session:
        benchmarks = [
            Benchmark(
                name="Reasoning Dashboard",
                ability=Ability.REASONING,
                language=Language.ZH,
                source="https://example.test/reasoning",
            ),
            Benchmark(
                name="Retrieval Dashboard",
                ability=Ability.RETRIEVAL,
                language=Language.EN,
                source="https://example.test/retrieval",
            ),
        ]
        session.add_all(benchmarks)
        await session.flush()
        session.add_all(
            [
                DatasetVersion(
                    benchmark_id=benchmarks[0].id,
                    version=1,
                    source_version="v1",
                    sampling_seed=1,
                    content_hash="a" * 64,
                    question_count=2,
                    status=DatasetStatus.FROZEN,
                    frozen_at=datetime.now(UTC),
                ),
                DatasetVersion(
                    benchmark_id=benchmarks[1].id,
                    version=1,
                    source_version="v1",
                    sampling_seed=1,
                    content_hash="b" * 64,
                    question_count=1,
                    status=DatasetStatus.FROZEN,
                    frozen_at=datetime.now(UTC),
                ),
                ScoreConfig(name="Dashboard Score"),
                JudgeConfig(
                    provider="dashboard-provider",
                    model="dashboard-judge",
                    prompt_version="v1",
                    enabled=True,
                ),
            ]
        )


@asynccontextmanager
async def dashboard_client(tmp_path: Path) -> AsyncIterator[tuple[AsyncClient, FastAPI]]:
    database_path = tmp_path / "dashboard.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        data_dir=tmp_path,
        environment="test",
        read_only_password_hash=password_hasher.hash(READ_ONLY_PASSWORD),
        operator_password_hash=password_hasher.hash(OPERATOR_PASSWORD),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await seed_dashboard(app.state.session_factory)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, app


async def login(client: AsyncClient, role: str, password: str) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://test"},
        json={"role": role, "password": password},
    )
    assert response.status_code == 200


def response_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for child in value.values() for key in response_keys(child)
        }
    if isinstance(value, list):
        return {key for child in value for key in response_keys(child)}
    return set()


async def test_dashboard_summary_is_authenticated_safe_and_available_to_both_roles(
    tmp_path: Path,
) -> None:
    async with dashboard_client(tmp_path) as (client, _):
        unauthenticated = await client.get("/api/v1/dashboard/summary")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["detail"]["code"] == "not_authenticated"

        await login(client, "read_only", READ_ONLY_PASSWORD)
        read_only = await client.get("/api/v1/dashboard/summary")
        assert read_only.status_code == 200
        assert read_only.headers["cache-control"] == "private, no-store"

        payload = read_only.json()
        assert payload == {
            "active_profile": {
                "key": "comprehensive",
                "name": "Comprehensive",
                "ready": True,
                "question_count": 3,
                "dataset_version_count": 2,
                "language_counts": {"zh": 2, "en": 1},
                "ability_counts": {
                    "reasoning": 2,
                    "retrieval": 1,
                    "data_reasoning": 0,
                    "instruction_following": 0,
                    "long_context": 0,
                },
                "ability_weights": {
                    "reasoning": 40,
                    "retrieval": 35,
                    "data_reasoning": 25,
                    "instruction_following": 0,
                    "long_context": 0,
                },
            },
            "ranking_profiles": [
                {
                    "key": "comprehensive",
                    "name": "Comprehensive",
                    "ready": True,
                    "question_count": 3,
                },
                {
                    "key": "reasoning",
                    "name": "Reasoning Specialist",
                    "ready": True,
                    "question_count": 2,
                },
                {
                    "key": "retrieval",
                    "name": "Retrieval Specialist",
                    "ready": True,
                    "question_count": 1,
                },
                {
                    "key": "data_reasoning",
                    "name": "Data Reasoning Specialist",
                    "ready": False,
                    "question_count": 0,
                },
            ],
            "judge_model": "dashboard-judge",
        }
        assert not {
            "id",
            "dataset_version_ids",
            "score_config_id",
            "hash",
            "issues",
            "provider",
        } & response_keys(payload)

        await login(client, "operator", OPERATOR_PASSWORD)
        operator = await client.get("/api/v1/dashboard/summary")
        assert operator.status_code == 200
        assert operator.headers["cache-control"] == "private, no-store"
        assert operator.json() == payload


async def test_dashboard_summary_marks_profiles_unready_without_a_judge(tmp_path: Path) -> None:
    async with dashboard_client(tmp_path) as (client, app):
        async with session_scope(app.state.session_factory) as session:
            judge = await session.scalar(select(JudgeConfig))
            assert judge is not None
            judge.enabled = False

        await login(client, "operator", OPERATOR_PASSWORD)
        response = await client.get("/api/v1/dashboard/summary")

        assert response.status_code == 200
        assert response.json()["judge_model"] is None
        assert response.json()["active_profile"]["ready"] is False
        assert all(not profile["ready"] for profile in response.json()["ranking_profiles"])
