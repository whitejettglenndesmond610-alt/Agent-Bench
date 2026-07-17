import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import joinedload

from agent_eval.api.main import create_app
from agent_eval.core.artifact_store import create_artifact_store
from agent_eval.core.settings import Settings
from agent_eval.datasets.balanced_builder import build_balanced_v2_dataset
from agent_eval.datasets.importer import BALANCED_V2_COHORT_KEY, import_question_records
from agent_eval.datasets.schema import load_question_records
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import (
    Ability,
    AgentStatus,
    AgentVersionLifecycle,
    AttemptStatus,
    AuthType,
    DatasetStatus,
    DesiredRunState,
    ExecutionStatus,
    Language,
    QuestionType,
    ResultValidityStatus,
    RunAgentStatus,
    RunScope,
    RunStage,
    RunStatus,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    Benchmark,
    DatasetVersion,
    EvaluationRun,
    ExecutionItem,
    HttpAttempt,
    JudgeConfig,
    LeaderboardResult,
    LeaderboardSnapshot,
    Question,
    RunAgent,
    RunDatasetVersion,
    ScoreConfig,
)
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.leaderboards.publisher import execute_aggregation_and_publish
from agent_eval.runs.lifecycle import (
    _recompute_run_agent_counts,
    apply_requested_cancellations,
    heartbeat_run,
    mark_run_system_failed,
    recover_stale_run,
)
from agent_eval.runs.profiles import build_run_profiles
from agent_eval.runs.queue import (
    acquire_worker_lease,
    claim_next_run,
    release_worker_lease,
)
from agent_eval.runs.service import (
    RunConfigurationError,
    metric_protocol_hash,
    prepare_evaluation_run,
    run_protocol_hash,
)
from agent_eval.worker.citation_scoring import (
    CitationScoreResult,
    CitationScoringWriter,
)
from agent_eval.worker.citations import CitationWriter
from agent_eval.worker.result_writer import ResultWriter
from agent_eval.worker.scoring import ScoreResult, ScoringWriter

OPERATOR_PASSWORD = "operator-secret"
READ_ONLY_PASSWORD = "readonly-secret"
password_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)


@dataclass(frozen=True)
class RunFixture:
    agent_version_ids: list[UUID]
    dataset_version_ids: list[UUID]
    judge_config_id: UUID
    score_config_id: UUID


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


async def seed_run_configuration(
    session_factory: async_sessionmaker,
) -> RunFixture:
    async with session_scope(session_factory) as session:
        agents = [
            Agent(name="Agent Alpha", provider="Provider", status=AgentStatus.ENABLED),
            Agent(name="Agent Beta", provider="Provider", status=AgentStatus.ENABLED),
        ]
        session.add_all(agents)
        await session.flush()
        agent_versions = [
            AgentVersion(
                agent_id=agent.id,
                version=1,
                endpoint_url=f"https://agent.example/{index}",
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
            )
            for index, agent in enumerate(agents)
        ]
        session.add_all(agent_versions)

        benchmarks = [
            Benchmark(
                name="Reasoning Demo",
                ability=Ability.REASONING,
                language=Language.ZH,
                source="https://example.test/reasoning",
            ),
            Benchmark(
                name="Retrieval Demo",
                ability=Ability.RETRIEVAL,
                language=Language.EN,
                source="https://example.test/retrieval",
            ),
        ]
        session.add_all(benchmarks)
        await session.flush()
        dataset_versions = [
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
        ]
        session.add_all(dataset_versions)
        await session.flush()
        session.add_all(
            [
                Question(
                    dataset_version_id=dataset_versions[0].id,
                    external_id="reasoning-1",
                    type=QuestionType.MULTIPLE_CHOICE,
                    prompt="Question one",
                    choices_json=[{"label": "A", "text": "One"}, {"label": "B", "text": "Two"}],
                    reference_answer="A",
                ),
                Question(
                    dataset_version_id=dataset_versions[0].id,
                    external_id="reasoning-2",
                    type=QuestionType.MULTIPLE_CHOICE,
                    prompt="Question two",
                    choices_json=[{"label": "A", "text": "One"}, {"label": "B", "text": "Two"}],
                    reference_answer="B",
                ),
                Question(
                    dataset_version_id=dataset_versions[1].id,
                    external_id="retrieval-1",
                    type=QuestionType.SHORT_ANSWER,
                    prompt="Find the answer",
                    reference_answer="Answer",
                    requires_citations=True,
                ),
            ]
        )
        judge = JudgeConfig(
            provider="deepseek", model="deepseek-chat", prompt_version="v1", enabled=True
        )
        score = ScoreConfig(name="MVP Default")
        session.add_all([judge, score])
        await session.flush()

        return RunFixture(
            agent_version_ids=[version.id for version in agent_versions],
            dataset_version_ids=[version.id for version in dataset_versions],
            judge_config_id=judge.id,
            score_config_id=score.id,
        )


@asynccontextmanager
async def run_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, FastAPI, Path, RunFixture]]:
    database_path = tmp_path / "runs-api.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        data_dir=tmp_path,
        environment="test",
        operator_password_hash=password_hasher.hash(OPERATOR_PASSWORD),
        read_only_password_hash=password_hasher.hash(READ_ONLY_PASSWORD),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        fixture = await seed_run_configuration(app.state.session_factory)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, app, database_path, fixture


async def login_operator(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://test"},
        json={"role": "operator", "password": OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    csrf_token = client.cookies.get("agent_eval_csrf")
    assert csrf_token is not None
    return {"Origin": "http://test", "X-CSRF-Token": csrf_token}


def run_payload(fixture: RunFixture, name: str = "Demo Evaluation") -> dict[str, object]:
    return {
        "name": name,
        "agent_version_ids": [str(fixture.agent_version_ids[0])],
        "dataset_version_ids": [str(value) for value in fixture.dataset_version_ids],
        "judge_config_id": str(fixture.judge_config_id),
        "score_config_id": str(fixture.score_config_id),
    }


def test_balanced_protocol_epoch_does_not_change_legacy_hash_payload() -> None:
    benchmark = Benchmark(
        id=uuid4(),
        name="Benchmark",
        ability=Ability.REASONING,
        language=Language.EN,
        source="source",
    )
    legacy = DatasetVersion(
        id=uuid4(),
        benchmark=benchmark,
        benchmark_id=benchmark.id,
        version=1,
        source_version="v1",
        content_hash="a" * 64,
        question_count=1,
        cohort_key="legacy-v1",
    )
    judge = JudgeConfig(provider="judge", model="model", prompt_version="v1")
    legacy_score = ScoreConfig(
        name="Legacy",
        reasoning_weight=40,
        retrieval_weight=35,
        data_weight=25,
        instruction_following_weight=0,
        long_context_weight=0,
        retrieval_answer_weight=70,
        retrieval_citation_weight=30,
        citation_authenticity_weight=50,
        citation_support_weight=50,
    )
    expected_payload = {
        "aggregation_version": "2",
        "datasets": [{"content_hash": legacy.content_hash, "id": str(legacy.id)}],
        "judge": {"model": "model", "prompt_version": "v1", "provider": "judge"},
        "profile": {
            "key": "comprehensive",
            "scope": "comprehensive",
            "target_ability": None,
        },
        "score": {
            "citation_authenticity_weight": 50,
            "citation_support_weight": 50,
            "data_weight": 25,
            "reasoning_weight": 40,
            "retrieval_answer_weight": 70,
            "retrieval_citation_weight": 30,
            "retrieval_weight": 35,
        },
    }
    expected_hash = hashlib.sha256(
        json.dumps(expected_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert run_protocol_hash(
        profile_key="comprehensive",
        run_scope=RunScope.COMPREHENSIVE,
        target_ability=None,
        dataset_versions=[legacy],
        judge_config=judge,
        score_config=legacy_score,
    ) == expected_hash

    balanced = DatasetVersion(
        id=legacy.id,
        benchmark=benchmark,
        benchmark_id=benchmark.id,
        version=1,
        source_version="v1",
        content_hash=legacy.content_hash,
        question_count=1,
        cohort_key=BALANCED_V2_COHORT_KEY,
    )
    balanced_score = ScoreConfig(
        name="Balanced",
        reasoning_weight=20,
        retrieval_weight=20,
        data_weight=20,
        instruction_following_weight=20,
        long_context_weight=20,
        retrieval_answer_weight=70,
        retrieval_citation_weight=30,
        citation_authenticity_weight=50,
        citation_support_weight=50,
    )
    assert run_protocol_hash(
        profile_key="balanced-v2",
        run_scope=RunScope.COMPREHENSIVE,
        target_ability=None,
        dataset_versions=[balanced],
        judge_config=judge,
        score_config=balanced_score,
    ) != expected_hash


async def test_run_options_report_limits_without_blocking_or_warning(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        async with session_scope(app.state.session_factory) as session:
            alpha = await session.get(AgentVersion, fixture.agent_version_ids[0])
            assert alpha is not None
            alpha.max_prompt_characters = 200
            retrieval_question = await session.scalar(
                select(Question).where(Question.external_id == "retrieval-1")
            )
            assert retrieval_question is not None
            retrieval_question.prompt = "x" * 300

        options_response = await client.get("/api/v1/evaluation-runs/options")
        assert options_response.status_code == 200
        agents = {agent["name"]: agent for agent in options_response.json()["agents"]}
        alpha_option = agents["Agent Alpha"]
        assert alpha_option["max_prompt_characters"] == 200
        assert alpha_option["supports_citations"] is False
        compatibility = {
            item["profile_key"]: item for item in alpha_option["profile_compatibility"]
        }
        assert compatibility["reasoning"]["compatible"] is True
        assert compatibility["retrieval"]["compatible"] is True
        assert compatibility["retrieval"]["oversized_question_count"] == 1
        assert compatibility["retrieval"]["citation_required_count"] == 1
        assert compatibility["retrieval"]["warnings"] == []
        assert compatibility["retrieval"]["blocking_reasons"] == []
        assert compatibility["comprehensive"]["compatible"] is True

        incompatible_payload = run_payload(fixture, "Incompatible Retrieval")
        incompatible_payload.pop("dataset_version_ids")
        incompatible_payload["profile_key"] = "retrieval"
        allowed = await client.post(
            "/api/v1/evaluation-runs",
            headers=mutation_headers,
            json=incompatible_payload,
        )
        assert allowed.status_code == 201
        assert allowed.json()["question_count"] == 1

        compatible_payload = run_payload(fixture, "Compatible Reasoning")
        compatible_payload.pop("dataset_version_ids")
        compatible_payload["profile_key"] = "reasoning"
        compatible = await client.post(
            "/api/v1/evaluation-runs",
            headers=mutation_headers,
            json=compatible_payload,
        )
        assert compatible.status_code == 201


async def test_unified_v3_options_hide_archived_profiles_and_create_default_run(
    tmp_path: Path,
) -> None:
    output = tmp_path / "balanced-v2"
    build_balanced_v2_dataset(Path("data/demo-dataset"), output)
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        async with session_scope(app.state.session_factory) as session:
            seeded_versions = list(
                await session.scalars(
                    select(DatasetVersion).where(
                        DatasetVersion.id.in_(fixture.dataset_version_ids)
                    )
                )
            )
            for version in seeded_versions:
                version.status = DatasetStatus.DRAFT
            await import_question_records(
                session, load_question_records(Path("data/demo-dataset"))
            )
            await import_question_records(
                session,
                load_question_records(output),
                cohort_key=BALANCED_V2_COHORT_KEY,
                allow_candidate=True,
            )

        options = await client.get("/api/v1/evaluation-runs/options")
        assert options.status_code == 200
        profiles = {profile["key"]: profile for profile in options.json()["profiles"]}
        unified = profiles["unified-v3"]
        assert set(profiles) == {
            "smoke",
            "unified-v3",
            *(f"unified-v3-{ability.value}" for ability in Ability),
        }
        assert options.json()["default_profile_key"] == "unified-v3"
        assert options.json()["question_count"] == 175
        assert options.json()["score_config_id"] == unified["score_config_id"]
        assert unified["question_count"] == 175
        assert unified["ability_weights"] == {ability.value: 20 for ability in Ability}
        assert {
            key for key in profiles if key.startswith("unified-v3")
        } == {
            "unified-v3",
            *(f"unified-v3-{ability.value}" for ability in Ability),
        }
        assert {
            ability.value: profiles[f"unified-v3-{ability.value}"]["question_count"]
            for ability in Ability
        } == {
            "reasoning": 45,
            "retrieval": 35,
            "data_reasoning": 35,
            "instruction_following": 30,
            "long_context": 30,
        }

        rejected_payload = run_payload(fixture, "Archived balanced profile")
        rejected_payload.pop("dataset_version_ids")
        rejected_payload["profile_key"] = "balanced-v2"
        rejected = await client.post(
            "/api/v1/evaluation-runs", headers=mutation_headers, json=rejected_payload
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "profile_not_found"

        default_payload = run_payload(fixture, "Unified V3 Default")
        default_payload.pop("dataset_version_ids")
        default_payload["score_config_id"] = unified["score_config_id"]
        default_created = await client.post(
            "/api/v1/evaluation-runs", headers=mutation_headers, json=default_payload
        )
        assert default_created.status_code == 201
        assert default_created.json()["profile_key"] == "unified-v3"
        assert default_created.json()["question_count"] == 175

        async with app.state.session_factory() as session:
            stored_profiles = {
                profile.key: profile
                for profile in await build_run_profiles(session)
            }
            comprehensive = stored_profiles["unified-v3"]
            specialist = stored_profiles["unified-v3-reasoning"]
            versions = list(
                await session.scalars(
                    select(DatasetVersion)
                    .options(joinedload(DatasetVersion.benchmark))
                    .where(DatasetVersion.id.in_(comprehensive.dataset_version_ids))
                )
            )
            judge = await session.get(JudgeConfig, fixture.judge_config_id)
            score = await session.get(ScoreConfig, comprehensive.score_config_id)
            assert judge is not None and score is not None
            comprehensive_metric_hash = metric_protocol_hash(
                metric="reasoning",
                dataset_versions=versions,
                judge_config=judge,
                score_config=score,
                profile_key=comprehensive.key,
            )
            specialist_versions = [
                version
                for version in versions
                if version.id in specialist.dataset_version_ids
            ]
            specialist_metric_hash = metric_protocol_hash(
                metric="reasoning",
                dataset_versions=specialist_versions,
                judge_config=judge,
                score_config=score,
                profile_key=specialist.key,
            )
            assert comprehensive_metric_hash == specialist_metric_hash
            assert run_protocol_hash(
                profile_key=comprehensive.key,
                run_scope=comprehensive.scope,
                target_ability=None,
                dataset_versions=versions,
                judge_config=judge,
                score_config=score,
            ) != run_protocol_hash(
                profile_key=specialist.key,
                run_scope=specialist.scope,
                target_ability=Ability.REASONING,
                dataset_versions=specialist_versions,
                judge_config=judge,
                score_config=score,
            )

            with pytest.raises(RunConfigurationError, match="完整且经过验证"):
                await prepare_evaluation_run(
                    session,
                    name="Partial unified",
                    agent_version_ids=[fixture.agent_version_ids[1]],
                    dataset_version_ids=list(comprehensive.dataset_version_ids[:-1]),
                    judge_config_id=judge.id,
                    score_config_id=score.id,
                    profile_key=comprehensive.key,
                )
            with pytest.raises(RunConfigurationError, match="cohort"):
                await prepare_evaluation_run(
                    session,
                    name="Arbitrary mixed",
                    agent_version_ids=[fixture.agent_version_ids[1]],
                    dataset_version_ids=[
                        next(
                            version.id
                            for version in versions
                            if version.cohort_key == "legacy-v1"
                        ),
                        next(
                            version.id
                            for version in versions
                            if version.cohort_key == BALANCED_V2_COHORT_KEY
                        ),
                    ],
                    judge_config_id=judge.id,
                    score_config_id=score.id,
                    profile_key="arbitrary-mixed",
                )


async def test_candidate_version_does_not_replace_active_evaluation_version(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        async with session_scope(app.state.session_factory) as session:
            active = await session.get(AgentVersion, fixture.agent_version_ids[0])
            assert active is not None
            candidate = AgentVersion(
                agent_id=active.agent_id,
                version=2,
                endpoint_url="https://agent.example/candidate",
                auth_type=AuthType.NONE,
                request_template=active.request_template,
                answer_jsonpath=active.answer_jsonpath,
                lifecycle_status=AgentVersionLifecycle.VERIFIED,
            )
            session.add(candidate)
            await session.flush()
            candidate_id = candidate.id

        options = await client.get("/api/v1/evaluation-runs/options")
        alpha = next(
            item
            for item in options.json()["agents"]
            if item["name"] == "Agent Alpha" and item["lifecycle_status"] == "active"
        )
        assert alpha["agent_version_id"] == str(fixture.agent_version_ids[0])
        assert alpha["version"] == 1
        candidate_option = next(
            item
            for item in options.json()["agents"]
            if item["agent_version_id"] == str(candidate_id)
        )
        assert candidate_option["lifecycle_status"] == "verified"
        candidate_profiles = {
            item["profile_key"]: item
            for item in candidate_option["profile_compatibility"]
        }
        assert candidate_profiles["comprehensive"]["compatible"] is False
        assert "候选版本只能运行非排名 Smoke" in candidate_profiles["comprehensive"][
            "blocking_reasons"
        ]

        statuses = await client.get("/api/v1/evaluation-runs/agent-statuses")
        alpha_status = next(
            item for item in statuses.json() if item["agent_name"] == "Agent Alpha"
        )
        assert alpha_status["agent_version_id"] == str(fixture.agent_version_ids[0])
        assert alpha_status["version"] == 1

        payload = run_payload(fixture, "Candidate must not run")
        payload["agent_version_ids"] = [str(candidate_id)]
        rejected = await client.post(
            "/api/v1/evaluation-runs",
            headers=mutation_headers,
            json=payload,
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "candidate_smoke_only"

        async with session_scope(app.state.session_factory) as session:
            smoke = await prepare_evaluation_run(
                session,
                name="Verified candidate smoke",
                agent_version_ids=[candidate_id],
                dataset_version_ids=fixture.dataset_version_ids,
                judge_config_id=fixture.judge_config_id,
                score_config_id=fixture.score_config_id,
                profile_key="smoke",
                run_scope=RunScope.SMOKE,
                ranking_eligible=False,
            )
            assert smoke.question_count == 3


async def test_run_batch_creates_independent_execution_matrices_transactionally(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)

        options = await client.get("/api/v1/evaluation-runs/options")
        assert options.status_code == 200
        assert options.json()["ready"] is True
        assert len(options.json()["agents"]) == 2
        assert len(options.json()["datasets"]) == 2
        assert options.json()["question_count"] == 3
        assert options.json()["default_profile_key"] == "comprehensive"
        profiles = {profile["key"]: profile for profile in options.json()["profiles"]}
        assert set(profiles) == {"comprehensive", "reasoning", "retrieval", "data_reasoning"}
        assert profiles["comprehensive"]["scope"] == "comprehensive"
        assert profiles["comprehensive"]["benchmark_count"] == 2
        assert len(profiles["comprehensive"]["datasets"]) == 2
        assert profiles["comprehensive"]["publishes_to"] == [
            "overall",
            "reasoning",
            "retrieval",
        ]
        assert profiles["reasoning"]["target_ability"] == "reasoning"
        assert [dataset["ability"] for dataset in profiles["reasoning"]["datasets"]] == [
            "reasoning"
        ]
        assert profiles["data_reasoning"]["dataset_version_ids"] == []
        assert options.json()["ability_weights"] == {
            "reasoning": 40,
            "retrieval": 35,
            "data_reasoning": 25,
            "instruction_following": 0,
            "long_context": 0,
        }

        batch_payload = run_payload(fixture)
        batch_payload["agent_version_ids"] = [str(value) for value in fixture.agent_version_ids]
        response = await client.post(
            "/api/v1/evaluation-runs/batch",
            headers=mutation_headers,
            json=batch_payload,
        )

        assert response.status_code == 201
        payloads = response.json()["runs"]
        assert len(payloads) == 2
        assert {payload["status"] for payload in payloads} == {"queued"}
        assert {payload["stage"] for payload in payloads} == {"preparing"}
        assert {payload["question_count"] for payload in payloads} == {3}
        assert {payload["execution_item_count"] for payload in payloads} == {3}
        assert all(len(payload["dataset_bundle_hash"]) == 64 for payload in payloads)
        assert {payload["profile_key"] for payload in payloads} == {"comprehensive"}
        assert {payload["run_scope"] for payload in payloads} == {"comprehensive"}
        assert {payload["target_ability"] for payload in payloads} == {None}
        assert {payload["ranking_eligible"] for payload in payloads} == {True}
        assert all(len(payload["protocol_hash"]) == 64 for payload in payloads)
        assert {payload["agent_version_ids"][0] for payload in payloads} == {
            str(value) for value in fixture.agent_version_ids
        }
        assert {payload["name"] for payload in payloads} == {
            "Demo Evaluation · Agent Alpha",
            "Demo Evaluation · Agent Beta",
        }

        runs = await client.get("/api/v1/evaluation-runs")
        progress = await client.get(f"/api/v1/evaluation-runs/{payloads[0]['id']}/progress")
        assert runs.status_code == 200
        assert len(runs.json()) == 2
        assert {run["agent_count"] for run in runs.json()} == {1}
        assert {run["total"] for run in runs.json()} == {3}
        assert progress.status_code == 200
        assert progress.json()["progress_percent"] == 0
        assert progress.json()["processed"] == 0
        assert len(progress.json()["agents"]) == 1
        assert {agent["total"] for agent in progress.json()["agents"]} == {3}
        assert progress.json()["agents"][0]["failure_categories"] == {}
        assert progress.json()["agents"][0]["validity_status"] == "pending"
        assert progress.json()["agents"][0]["validity_reason"] is None
        assert progress.json()["agents"][0]["configuration_failure_count"] == 0
        assert progress.json()["agents"][0]["configuration_failure_rate"] == 0

        async with app.state.session_factory() as session:
            run_count = await session.scalar(select(func.count()).select_from(EvaluationRun))
            link_count = await session.scalar(select(func.count()).select_from(RunDatasetVersion))
            run_agents = list(await session.scalars(select(RunAgent)))
            execution_items = list(await session.scalars(select(ExecutionItem)))
            result_run_agent = next(
                item for item in run_agents if str(item.run_id) == payloads[0]["id"]
            )
            session.add(
                LeaderboardSnapshot(
                    run_id=result_run_agent.run_id,
                    published_at=datetime.now(UTC),
                    rankings_json=[
                        {
                            "agent_version_id": str(result_run_agent.agent_version_id),
                            "overall_score": 72.5,
                            "ability_scores": {
                                "reasoning": 80,
                                "retrieval": 65,
                                "data_reasoning": 70,
                            },
                            "answer_quality": 75,
                            "citation_quality": 60,
                            "success_rate": 98,
                            "anomaly_rate": 2,
                            "http_call_count": 4,
                            "total_input_tokens": 100,
                            "total_output_tokens": 25,
                            "total_tokens": 125,
                            "token_usage_available_count": 3,
                            "token_usage_missing_count": 1,
                            "latency_p50_ms": 75,
                            "latency_p95_ms": 140,
                            "latency_sample_count": 4,
                        }
                    ],
                )
            )
            await session.commit()

        assert run_count == 2
        assert link_count == 4
        assert len(run_agents) == 2
        assert {run_agent.total for run_agent in run_agents} == {3}
        assert {run_agent.status for run_agent in run_agents} == {RunAgentStatus.QUEUED}
        assert len(execution_items) == 6
        assert {item.status for item in execution_items} == {ExecutionStatus.PENDING}
        result_progress = await client.get(
            f"/api/v1/evaluation-runs/{payloads[0]['id']}/progress"
        )
        assert result_progress.json()["agents"][0]["result"]["overall_score"] == 72.5
        assert result_progress.json()["agents"][0]["result"]["validity_status"] == "pending"
        result_metrics = result_progress.json()["agents"][0]["result"]
        assert result_metrics["total_input_tokens"] == 100
        assert result_metrics["total_output_tokens"] == 25
        assert result_metrics["total_tokens"] == 125
        assert result_metrics["token_usage_available_count"] == 3
        assert result_metrics["token_usage_missing_count"] == 1
        assert result_metrics["latency_p50_ms"] == 75
        assert result_metrics["latency_p95_ms"] == 140
        assert result_metrics["latency_sample_count"] == 4


async def test_run_api_allows_one_agent_and_server_resolves_default_profile(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, _, _, fixture):
        mutation_headers = await login_operator(client)
        payload = run_payload(fixture)
        payload["agent_version_ids"] = [str(fixture.agent_version_ids[0])]
        payload.pop("dataset_version_ids")

        response = await client.post(
            "/api/v1/evaluation-runs", headers=mutation_headers, json=payload
        )

        assert response.status_code == 201
        assert response.json()["question_count"] == 3
        assert response.json()["execution_item_count"] == 3
        assert set(response.json()["dataset_version_ids"]) == {
            str(value) for value in fixture.dataset_version_ids
        }


async def test_public_run_api_rejects_multiple_agents(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        payload = run_payload(fixture)
        payload["agent_version_ids"] = [str(value) for value in fixture.agent_version_ids]

        response = await client.post(
            "/api/v1/evaluation-runs", headers=mutation_headers, json=payload
        )

        assert response.status_code == 422
        async with app.state.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(EvaluationRun)) == 0


async def test_batch_accepts_one_agent(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, _, _, fixture):
        mutation_headers = await login_operator(client)

        response = await client.post(
            "/api/v1/evaluation-runs/batch",
            headers=mutation_headers,
            json=run_payload(fixture, "Single Batch"),
        )

        assert response.status_code == 201
        assert len(response.json()["runs"]) == 1
        assert response.json()["runs"][0]["name"] == "Single Batch · Agent Alpha"


async def test_run_api_rejects_profile_dataset_mismatch(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        payload = run_payload(fixture)
        payload["profile_key"] = "reasoning"

        response = await client.post(
            "/api/v1/evaluation-runs", headers=mutation_headers, json=payload
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "profile_dataset_mismatch"
        async with app.state.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(EvaluationRun)) == 0

        payload.pop("dataset_version_ids")
        accepted = await client.post(
            "/api/v1/evaluation-runs", headers=mutation_headers, json=payload
        )
        assert accepted.status_code == 201
        assert accepted.json()["profile_key"] == "reasoning"
        assert accepted.json()["run_scope"] == "specialist"
        assert accepted.json()["target_ability"] == "reasoning"
        assert accepted.json()["question_count"] == 2


async def test_run_api_schema_rejects_two_versions_of_same_logical_agent(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        async with session_scope(app.state.session_factory) as session:
            first = await session.get(AgentVersion, fixture.agent_version_ids[0])
            assert first is not None
            second = AgentVersion(
                agent_id=first.agent_id,
                version=2,
                endpoint_url="https://agent.example/second",
                auth_type=AuthType.NONE,
                request_template=first.request_template,
                answer_jsonpath=first.answer_jsonpath,
            )
            session.add(second)
            await session.flush()
            second_id = second.id

        mutation_headers = await login_operator(client)
        payload = run_payload(fixture)
        payload["agent_version_ids"] = [str(fixture.agent_version_ids[0]), str(second_id)]
        response = await client.post(
            "/api/v1/evaluation-runs", headers=mutation_headers, json=payload
        )

        assert response.status_code == 422
        async with app.state.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(EvaluationRun)) == 0


async def test_active_duplicate_is_rejected_and_canceled_run_can_retry(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        payload = run_payload(fixture)
        created = await client.post(
            "/api/v1/evaluation-runs", headers=mutation_headers, json=payload
        )
        assert created.status_code == 201

        duplicate = await client.post(
            "/api/v1/evaluation-runs", headers=mutation_headers, json=payload
        )
        assert duplicate.status_code == 422
        assert duplicate.json()["detail"]["code"] == "evaluation_already_active"

        async with session_scope(app.state.session_factory) as session:
            run = await session.get(EvaluationRun, UUID(created.json()["id"]))
            assert run is not None
            run.status = RunStatus.CANCELED
            run.finished_at = datetime.now(UTC)

        retry = await client.post("/api/v1/evaluation-runs", headers=mutation_headers, json=payload)
        assert retry.status_code == 201


async def test_published_duplicate_requires_force_retest(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        payload = run_payload(fixture)
        created = await client.post(
            "/api/v1/evaluation-runs", headers=mutation_headers, json=payload
        )
        assert created.status_code == 201
        async with session_scope(app.state.session_factory) as session:
            run = await session.get(EvaluationRun, UUID(created.json()["id"]))
            assert run is not None
            run.status = RunStatus.PUBLISHED
            run.finished_at = datetime.now(UTC)
            run.protocol_hash = "legacy-protocol"
            datasets = list(
                await session.scalars(
                    select(DatasetVersion)
                    .options(joinedload(DatasetVersion.benchmark))
                    .where(DatasetVersion.id.in_(fixture.dataset_version_ids))
                )
            )
            judge = await session.get(JudgeConfig, fixture.judge_config_id)
            score_config = await session.get(ScoreConfig, fixture.score_config_id)
            run_agent = await session.scalar(
                select(RunAgent).where(RunAgent.run_id == run.id)
            )
            version = await session.get(AgentVersion, fixture.agent_version_ids[0])
            assert judge is not None and score_config is not None
            assert run_agent is not None and version is not None
            snapshot = LeaderboardSnapshot(
                run_id=run.id,
                rankings_json=[],
                is_current=False,
            )
            session.add(snapshot)
            await session.flush()
            session.add(
                LeaderboardResult(
                    snapshot_id=snapshot.id,
                    run_agent_id=run_agent.id,
                    agent_id=version.agent_id,
                    agent_version_id=version.id,
                    metric="overall",
                    score=Decimal("80"),
                    protocol_hash=metric_protocol_hash(
                        metric="overall",
                        dataset_versions=datasets,
                        judge_config=judge,
                        score_config=score_config,
                    ),
                    benchmark_count=len(datasets),
                )
            )

        duplicate = await client.post(
            "/api/v1/evaluation-runs", headers=mutation_headers, json=payload
        )
        assert duplicate.status_code == 422
        assert duplicate.json()["detail"]["code"] == "evaluation_result_exists"

        payload["force_retest"] = True
        forced = await client.post(
            "/api/v1/evaluation-runs", headers=mutation_headers, json=payload
        )
        assert forced.status_code == 201
        assert forced.json()["id"] != created.json()["id"]


async def test_batch_rolls_back_when_one_agent_is_duplicate(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        active = await client.post(
            "/api/v1/evaluation-runs",
            headers=mutation_headers,
            json=run_payload(fixture),
        )
        assert active.status_code == 201

        payload = run_payload(fixture, "Atomic Batch")
        payload["agent_version_ids"] = [
            str(fixture.agent_version_ids[1]),
            str(fixture.agent_version_ids[0]),
        ]
        rejected = await client.post(
            "/api/v1/evaluation-runs/batch", headers=mutation_headers, json=payload
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "evaluation_already_active"

        async with app.state.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(EvaluationRun)) == 1
            assert await session.scalar(select(func.count()).select_from(RunAgent)) == 1
            assert await session.scalar(select(func.count()).select_from(ExecutionItem)) == 3


async def test_leaderboard_query_and_same_snapshot_compare_apis(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        created = await client.post(
            "/api/v1/evaluation-runs",
            headers=mutation_headers,
            json=run_payload(fixture, "Published Evaluation"),
        )
        run_id = UUID(created.json()["id"])
        benchmark_id = uuid4()

        def ranking(position: int, agent_version_id: UUID) -> dict[str, object]:
            score = float(90 - position * 10)
            return {
                "rank": position,
                "run_agent_id": str(uuid4()),
                "agent_id": str(uuid4()),
                "agent_version_id": str(agent_version_id),
                "agent_name": f"Agent {position}",
                "provider": "Provider",
                "version": 1,
                "overall_score": score,
                "ability_scores": {
                    "reasoning": score,
                    "retrieval": score,
                    "data_reasoning": score,
                },
                "language_scores": {"zh": score, "en": score},
                "benchmark_scores": [
                    {
                        "benchmark_id": str(benchmark_id),
                        "name": "Benchmark",
                        "ability": "reasoning",
                        "language": "zh",
                        "score": score,
                    }
                ],
                "answer_quality": score,
                "citation_quality": score,
                "success_rate": 100.0,
                "anomaly_rate": 0.0,
                "http_call_count": position,
            }

        rankings = [
            ranking(index, version_id)
            for index, version_id in enumerate(fixture.agent_version_ids, start=1)
        ]
        async with session_scope(app.state.session_factory) as session:
            run = await session.get(EvaluationRun, run_id)
            assert run is not None
            run.profile_key = "unified-v3"
            for run_agent in await session.scalars(
                select(RunAgent).where(RunAgent.run_id == run_id)
            ):
                run_agent.validity_status = ResultValidityStatus.VALID
            snapshot = LeaderboardSnapshot(
                run_id=run_id,
                rankings_json=rankings,
                is_current=True,
            )
            session.add(snapshot)
            await session.flush()
            snapshot_id = snapshot.id

        current = await client.get("/api/v1/leaderboards/current")
        history = await client.get("/api/v1/leaderboards/history")
        historical = await client.get(f"/api/v1/leaderboards/{snapshot_id}")
        compared = await client.get(
            f"/api/v1/leaderboards/{snapshot_id}/compare",
            params=[
                ("agent_version_ids", str(version_id)) for version_id in fixture.agent_version_ids
            ],
        )

        assert current.status_code == 200
        assert current.json()["run_name"] == "Published Evaluation"
        assert len(current.json()["rankings"]) == 2
        historical_metrics = current.json()["rankings"][0]
        assert historical_metrics["total_input_tokens"] is None
        assert historical_metrics["total_output_tokens"] is None
        assert historical_metrics["total_tokens"] is None
        assert historical_metrics["token_usage_available_count"] == 0
        assert historical_metrics["token_usage_missing_count"] == 0
        assert historical_metrics["latency_p50_ms"] is None
        assert historical_metrics["latency_p95_ms"] is None
        assert historical_metrics["latency_sample_count"] == 0
        assert history.status_code == 200
        assert history.json() == [
            {
                "id": str(snapshot_id),
                "run_id": str(run_id),
                "run_name": "Published Evaluation",
                "published_at": history.json()[0]["published_at"],
                "agent_count": 2,
                "is_current": True,
            }
        ]
        assert historical.status_code == 200
        assert historical.json()["id"] == str(snapshot_id)
        assert compared.status_code == 200
        assert {item["agent_version_id"] for item in compared.json()["rankings"]} == {
            str(value) for value in fixture.agent_version_ids
        }

        async with session_scope(app.state.session_factory) as session:
            version = await session.get(AgentVersion, fixture.agent_version_ids[0])
            assert version is not None
            version.enabled = False

        rejected = await client.post(
            "/api/v1/evaluation-runs",
            headers=mutation_headers,
            json=run_payload(fixture, "Rejected Evaluation"),
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "agent_version_disabled"
        async with app.state.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(EvaluationRun)) == 1

        readonly_login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://test"},
            json={"role": "read_only", "password": READ_ONLY_PASSWORD},
        )
        assert readonly_login.status_code == 200
        readonly_csrf = client.cookies.get("agent_eval_csrf")
        assert readonly_csrf is not None
        assert (await client.get("/api/v1/leaderboards/current")).status_code == 200
        assert (await client.get("/api/v1/leaderboards/history")).status_code == 200
        assert (await client.get(f"/api/v1/leaderboards/{snapshot_id}")).status_code == 200
        assert (await client.get("/api/v1/agents")).status_code == 403
        assert (await client.get("/api/v1/evaluation-runs")).status_code == 403
        readonly_mutation = await client.post(
            "/api/v1/evaluation-runs",
            headers={"Origin": "http://test", "X-CSRF-Token": readonly_csrf},
            json=run_payload(fixture, "Forbidden Evaluation"),
        )
        assert readonly_mutation.status_code == 403


async def test_unified_epoch_ignores_historical_results_but_preserves_history(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        created = await client.post(
            "/api/v1/evaluation-runs",
            headers=mutation_headers,
            json=run_payload(fixture, "Legacy historical result"),
        )
        run_id = UUID(created.json()["id"])
        async with session_scope(app.state.session_factory) as session:
            run_agent = await session.scalar(select(RunAgent).where(RunAgent.run_id == run_id))
            version = await session.get(AgentVersion, fixture.agent_version_ids[0])
            assert run_agent is not None and version is not None
            snapshot = LeaderboardSnapshot(
                run_id=run_id,
                rankings_json=[],
                is_current=True,
            )
            session.add(snapshot)
            await session.flush()
            session.add(
                LeaderboardResult(
                    snapshot_id=snapshot.id,
                    run_agent_id=run_agent.id,
                    agent_id=version.agent_id,
                    agent_version_id=version.id,
                    metric="overall",
                    score=Decimal("99"),
                    protocol_hash="f" * 64,
                    benchmark_count=2,
                )
            )
            snapshot_id = snapshot.id

        for metric in (
            "overall",
            "reasoning",
            "retrieval",
            "data_reasoning",
            "instruction_following",
            "long_context",
        ):
            response = await client.get(f"/api/v1/leaderboards/global/{metric}")
            assert response.status_code == 200
            assert response.json()["protocol_hash"] is None
            assert response.json()["ranked_agents"] == []
        assert (await client.get("/api/v1/leaderboards/current")).status_code == 404
        history = await client.get("/api/v1/leaderboards/history")
        assert history.status_code == 200
        assert history.json()[0]["id"] == str(snapshot_id)
        historical = await client.get(f"/api/v1/leaderboards/{snapshot_id}")
        assert historical.status_code == 200
        statuses = await client.get("/api/v1/evaluation-runs/agent-statuses")
        legacy_agent = next(
            item for item in statuses.json() if item["agent_name"] == "Agent Alpha"
        )
        assert {
            legacy_agent[key]["status"]
            for key in (
                "comprehensive",
                "reasoning",
                "retrieval",
                "data_reasoning",
                "instruction_following",
                "long_context",
            )
        } == {"not_evaluated"}

        async with session_scope(app.state.session_factory) as session:
            historical_run = await session.get(EvaluationRun, run_id)
            assert historical_run is not None
            historical_run.status = RunStatus.SYSTEM_FAILED
            historical_run.system_error = "legacy failure"
        failed_statuses = await client.get("/api/v1/evaluation-runs/agent-statuses")
        failed_legacy_agent = next(
            item
            for item in failed_statuses.json()
            if item["agent_name"] == "Agent Alpha"
        )
        assert failed_legacy_agent["comprehensive"]["status"] == "not_evaluated"


async def test_global_leaderboard_unions_compatible_runs_and_tracks_agent_versions(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        await login_operator(client)
        async with session_scope(app.state.session_factory) as session:
            alpha_v1 = await session.get(AgentVersion, fixture.agent_version_ids[0])
            beta_v1 = await session.get(AgentVersion, fixture.agent_version_ids[1])
            assert alpha_v1 is not None and beta_v1 is not None
            alpha_v2 = AgentVersion(
                agent_id=alpha_v1.agent_id,
                version=2,
                endpoint_url="https://agent.example/alpha-v2",
                auth_type=AuthType.NONE,
                request_template=alpha_v1.request_template,
                answer_jsonpath=alpha_v1.answer_jsonpath,
            )
            alpha_v3 = AgentVersion(
                agent_id=alpha_v1.agent_id,
                version=3,
                endpoint_url="https://agent.example/alpha-v3",
                auth_type=AuthType.NONE,
                request_template=alpha_v1.request_template,
                answer_jsonpath=alpha_v1.answer_jsonpath,
            )
            gamma = Agent(name="Agent Gamma", provider="Provider", status=AgentStatus.ENABLED)
            session.add_all([alpha_v2, alpha_v3, gamma])
            await session.flush()
            gamma_v1 = AgentVersion(
                agent_id=gamma.id,
                version=1,
                endpoint_url="https://agent.example/gamma",
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
            )
            session.add(gamma_v1)
            await session.flush()

            judge = await session.get(JudgeConfig, fixture.judge_config_id)
            score_config = await session.get(ScoreConfig, fixture.score_config_id)
            assert judge is not None and score_config is not None
            protocol = "a" * 64
            incompatible_protocol = "b" * 64

            def ranking(
                run_agent: RunAgent,
                version: AgentVersion,
                score: float,
            ) -> dict[str, object]:
                result: dict[str, object] = {
                    "rank": 1,
                    "run_agent_id": str(run_agent.id),
                    "agent_id": str(version.agent_id),
                    "agent_version_id": str(version.id),
                    "agent_name": "unused snapshot name",
                    "provider": "unused snapshot provider",
                    "version": version.version,
                    "overall_score": score,
                    "ability_scores": {
                        "reasoning": score,
                        "retrieval": score - 10,
                        "data_reasoning": 0,
                    },
                    "language_scores": {"zh": score, "en": score - 10},
                    "benchmark_scores": [
                        {
                            "benchmark_id": str(uuid4()),
                            "name": "Reasoning Benchmark",
                            "ability": "reasoning",
                            "language": "zh",
                            "score": score,
                        },
                        {
                            "benchmark_id": str(uuid4()),
                            "name": "Retrieval Benchmark",
                            "ability": "retrieval",
                            "language": "en",
                            "score": score - 10,
                        },
                    ],
                    "answer_quality": score,
                    "citation_quality": 0,
                    "success_rate": 100,
                    "anomaly_rate": 0,
                    "http_call_count": version.version,
                }
                if version.agent_id == alpha_v1.agent_id:
                    result.update(
                        {
                            "total_input_tokens": 120,
                            "total_output_tokens": 30,
                            "total_tokens": 150,
                            "token_usage_available_count": 2,
                            "token_usage_missing_count": 1,
                            "latency_p50_ms": 80,
                            "latency_p95_ms": 140,
                            "latency_sample_count": 3,
                        }
                    )
                return result

            async def add_source(
                name: str,
                published_at: datetime,
                sources: list[tuple[AgentVersion, Decimal]],
                source_protocol: str,
                *,
                link_datasets: bool = False,
            ) -> None:
                run = EvaluationRun(
                    name=name,
                    status=RunStatus.PUBLISHED,
                    stage=RunStage.READY_TO_PUBLISH,
                    dataset_bundle_hash="c" * 64,
                    profile_key="unified-v3-reasoning",
                    run_scope=RunScope.SPECIALIST,
                    target_ability=Ability.REASONING,
                    ranking_eligible=True,
                    protocol_hash=source_protocol,
                    judge_config_id=judge.id,
                    score_config_id=score_config.id,
                    finished_at=published_at,
                )
                session.add(run)
                await session.flush()
                if link_datasets:
                    session.add_all(
                        [
                            RunDatasetVersion(run_id=run.id, dataset_version_id=dataset_id)
                            for dataset_id in fixture.dataset_version_ids
                        ]
                    )
                run_agents = [
                    RunAgent(
                        run_id=run.id,
                        agent_version_id=version.id,
                        status=RunAgentStatus.COMPLETED,
                        total=1,
                        completed=1,
                        validity_status=ResultValidityStatus.VALID,
                    )
                    for version, _ in sources
                ]
                session.add_all(run_agents)
                await session.flush()
                snapshot = LeaderboardSnapshot(
                    run_id=run.id,
                    published_at=published_at,
                    rankings_json=[
                        ranking(run_agent, version, float(result_score))
                        for run_agent, (version, result_score) in zip(
                            run_agents, sources, strict=True
                        )
                    ],
                    is_current=False,
                )
                session.add(snapshot)
                await session.flush()
                session.add_all(
                    [
                        LeaderboardResult(
                            snapshot_id=snapshot.id,
                            run_agent_id=run_agent.id,
                            agent_id=version.agent_id,
                            agent_version_id=version.id,
                            metric="reasoning",
                            score=result_score,
                            protocol_hash=source_protocol,
                            benchmark_count=1,
                        )
                        for run_agent, (version, result_score) in zip(
                            run_agents, sources, strict=True
                        )
                    ]
                )

            await add_source(
                "Alpha v2 compatible",
                datetime(2026, 1, 1, tzinfo=UTC),
                [(alpha_v2, Decimal("90.000000000000000001"))],
                protocol,
            )
            await add_source(
                "Incompatible",
                datetime(2026, 1, 2, tzinfo=UTC),
                [(alpha_v3, Decimal("99"))],
                incompatible_protocol,
            )
            await add_source(
                "Newest compatible",
                datetime(2026, 1, 3, tzinfo=UTC),
                [
                    (alpha_v1, Decimal("95")),
                    (beta_v1, Decimal("90.000000000000000001")),
                ],
                protocol,
                link_datasets=True,
            )

        response = await client.get("/api/v1/leaderboards/global/reasoning")
        assert response.status_code == 200
        payload = response.json()
        assert payload["metric"] == "reasoning"
        assert payload["label"] == "推理"
        assert payload["protocol_hash"] == protocol
        assert payload["eligible_agent_count"] == 3
        assert payload["benchmark_count"] == 1
        assert payload["question_count"] == 2
        assert [item["agent_name"] for item in payload["ranked_agents"]] == [
            "Agent Alpha",
            "Agent Beta",
        ]
        assert [item["rank"] for item in payload["ranked_agents"]] == [1, 1]
        assert payload["ranked_agents"][0]["agent_version_id"] == str(alpha_v2.id)
        assert payload["ranked_agents"][0]["status"] == "needs_retest"
        assert payload["ranked_agents"][1]["status"] == "current"
        assert payload["ranked_agents"][0]["total_input_tokens"] == 120
        assert payload["ranked_agents"][0]["total_output_tokens"] == 30
        assert payload["ranked_agents"][0]["total_tokens"] == 150
        assert payload["ranked_agents"][0]["latency_p50_ms"] == 80
        assert payload["ranked_agents"][0]["latency_p95_ms"] == 140
        assert payload["ranked_agents"][0]["latency_sample_count"] == 3
        assert payload["ranked_agents"][0]["token_usage_available_count"] == 2
        assert payload["ranked_agents"][0]["token_usage_missing_count"] == 1
        assert payload["ranked_agents"][1]["total_tokens"] is None
        assert payload["ranked_agents"][1]["latency_sample_count"] == 0
        assert all(
            benchmark["ability"] == "reasoning"
            for item in payload["ranked_agents"]
            for benchmark in item["benchmark_scores"]
        )
        assert payload["unranked_agents"] == [
            {
                "agent_id": str(gamma.id),
                "agent_version_id": str(gamma_v1.id),
                "agent_name": "Agent Gamma",
                "provider": "Provider",
                "version": 1,
                "status": "not_evaluated",
            }
        ]

        overview_response = await client.get("/api/v1/leaderboards/global/overview")
        assert overview_response.status_code == 200
        overview = overview_response.json()
        assert overview["eligible_agent_count"] == 3
        assert [protocol["metric"] for protocol in overview["protocols"]] == [
            "overall",
            "reasoning",
            "retrieval",
            "data_reasoning",
            "instruction_following",
            "long_context",
        ]
        overview_by_name = {item["agent_name"]: item for item in overview["agents"]}
        alpha_overview = overview_by_name["Agent Alpha"]
        assert alpha_overview["agent_version_id"] == str(alpha_v3.id)
        assert alpha_overview["metrics"]["reasoning"]["rank"] == 1
        assert alpha_overview["metrics"]["reasoning"]["score"] == 90.0
        assert alpha_overview["metrics"]["reasoning"]["status"] == "needs_retest"
        assert alpha_overview["metrics"]["reasoning"]["total_input_tokens"] == 120
        assert alpha_overview["metrics"]["reasoning"]["total_output_tokens"] == 30
        assert alpha_overview["metrics"]["reasoning"]["total_tokens"] == 150
        assert alpha_overview["metrics"]["reasoning"]["latency_p50_ms"] == 80
        assert alpha_overview["metrics"]["reasoning"]["latency_p95_ms"] == 140
        assert alpha_overview["metrics"]["reasoning"]["latency_sample_count"] == 3
        assert alpha_overview["metrics"]["reasoning"]["source_agent_version_id"] == str(
            alpha_v2.id
        )
        assert alpha_overview["metrics"]["overall"]["status"] == "not_evaluated"
        assert alpha_overview["metrics"]["instruction_following"]["status"] == "not_evaluated"
        assert alpha_overview["metrics"]["long_context"]["status"] == "not_evaluated"
        assert overview_by_name["Agent Gamma"]["metrics"]["reasoning"]["rank"] is None

        comparison = await client.get(
            "/api/v1/leaderboards/global/compare",
            params=[
                ("agent_ids", str(alpha_v1.agent_id)),
                ("agent_ids", str(beta_v1.agent_id)),
            ],
        )
        assert comparison.status_code == 200
        assert {item["agent_name"] for item in comparison.json()["agents"]} == {
            "Agent Alpha",
            "Agent Beta",
        }

        duplicate_comparison = await client.get(
            "/api/v1/leaderboards/global/compare",
            params=[
                ("agent_ids", str(alpha_v1.agent_id)),
                ("agent_ids", str(alpha_v1.agent_id)),
            ],
        )
        assert duplicate_comparison.status_code == 422
        assert duplicate_comparison.json()["detail"]["code"] == "duplicate_agents"

        readonly_login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://test"},
            json={"role": "read_only", "password": READ_ONLY_PASSWORD},
        )
        assert readonly_login.status_code == 200
        assert (await client.get("/api/v1/leaderboards/global/reasoning")).status_code == 200
        assert (await client.get("/api/v1/leaderboards/global/overview")).status_code == 200


async def test_agent_status_matrix_combines_results_active_runs_and_failures(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        await login_operator(client)
        now = datetime.now(UTC)
        async with session_scope(app.state.session_factory) as session:
            alpha_v1 = await session.get(AgentVersion, fixture.agent_version_ids[0])
            beta_v1 = await session.get(AgentVersion, fixture.agent_version_ids[1])
            judge = await session.get(JudgeConfig, fixture.judge_config_id)
            score_config = await session.get(ScoreConfig, fixture.score_config_id)
            assert alpha_v1 is not None and beta_v1 is not None
            assert judge is not None and score_config is not None
            alpha_v2 = AgentVersion(
                agent_id=alpha_v1.agent_id,
                version=2,
                endpoint_url="https://agent.example/alpha-v2",
                auth_type=AuthType.NONE,
                request_template=alpha_v1.request_template,
                answer_jsonpath=alpha_v1.answer_jsonpath,
            )
            session.add(alpha_v2)
            await session.flush()

            alpha_result_run = EvaluationRun(
                name="Alpha historical comprehensive",
                status=RunStatus.PUBLISHED,
                stage=RunStage.READY_TO_PUBLISH,
                dataset_bundle_hash="a" * 64,
                profile_key="unified-v3",
                run_scope=RunScope.COMPREHENSIVE,
                ranking_eligible=True,
                protocol_hash="b" * 64,
                judge_config_id=judge.id,
                score_config_id=score_config.id,
                finished_at=now - timedelta(minutes=4),
            )
            beta_result_run = EvaluationRun(
                name="Beta reasoning",
                status=RunStatus.PUBLISHED,
                stage=RunStage.READY_TO_PUBLISH,
                dataset_bundle_hash="c" * 64,
                profile_key="unified-v3-reasoning",
                run_scope=RunScope.SPECIALIST,
                target_ability=Ability.REASONING,
                ranking_eligible=True,
                protocol_hash="d" * 64,
                judge_config_id=judge.id,
                score_config_id=score_config.id,
                finished_at=now - timedelta(minutes=3),
            )
            session.add_all([alpha_result_run, beta_result_run])
            await session.flush()
            alpha_result_agent = RunAgent(
                run_id=alpha_result_run.id,
                agent_version_id=alpha_v1.id,
                status=RunAgentStatus.COMPLETED,
                validity_status=ResultValidityStatus.VALID,
            )
            beta_result_agent = RunAgent(
                run_id=beta_result_run.id,
                agent_version_id=beta_v1.id,
                status=RunAgentStatus.COMPLETED,
                validity_status=ResultValidityStatus.VALID,
            )
            session.add_all([alpha_result_agent, beta_result_agent])
            await session.flush()
            alpha_snapshot = LeaderboardSnapshot(
                run_id=alpha_result_run.id,
                published_at=now - timedelta(minutes=4),
                rankings_json=[],
            )
            beta_snapshot = LeaderboardSnapshot(
                run_id=beta_result_run.id,
                published_at=now - timedelta(minutes=3),
                rankings_json=[],
            )
            session.add_all([alpha_snapshot, beta_snapshot])
            await session.flush()
            session.add_all(
                [
                    LeaderboardResult(
                        snapshot_id=alpha_snapshot.id,
                        run_agent_id=alpha_result_agent.id,
                        agent_id=alpha_v1.agent_id,
                        agent_version_id=alpha_v1.id,
                        metric="overall",
                        score=Decimal("91"),
                        protocol_hash="e" * 64,
                        benchmark_count=2,
                    ),
                    LeaderboardResult(
                        snapshot_id=beta_snapshot.id,
                        run_agent_id=beta_result_agent.id,
                        agent_id=beta_v1.agent_id,
                        agent_version_id=beta_v1.id,
                        metric="reasoning",
                        score=Decimal("87"),
                        protocol_hash="f" * 64,
                        benchmark_count=1,
                    ),
                ]
            )
            active = EvaluationRun(
                name="Alpha retrieval active",
                status=RunStatus.QUEUED,
                dataset_bundle_hash="1" * 64,
                profile_key="unified-v3-retrieval",
                run_scope=RunScope.SPECIALIST,
                target_ability=Ability.RETRIEVAL,
                ranking_eligible=True,
                protocol_hash="2" * 64,
                judge_config_id=judge.id,
                score_config_id=score_config.id,
            )
            failed = EvaluationRun(
                name="Beta comprehensive failed",
                status=RunStatus.SYSTEM_FAILED,
                dataset_bundle_hash="3" * 64,
                profile_key="unified-v3",
                run_scope=RunScope.COMPREHENSIVE,
                ranking_eligible=True,
                protocol_hash="4" * 64,
                judge_config_id=judge.id,
                score_config_id=score_config.id,
                finished_at=now,
                system_error="judge_unavailable",
            )
            session.add_all([active, failed])
            await session.flush()
            session.add_all(
                [
                    RunAgent(run_id=active.id, agent_version_id=alpha_v2.id),
                    RunAgent(run_id=failed.id, agent_version_id=beta_v1.id),
                ]
            )

        response = await client.get("/api/v1/evaluation-runs/agent-statuses")
        assert response.status_code == 200
        by_name = {item["agent_name"]: item for item in response.json()}
        alpha = by_name["Agent Alpha"]
        beta = by_name["Agent Beta"]
        assert alpha["version"] == 2
        assert alpha["comprehensive"]["status"] == "needs_retest"
        assert alpha["comprehensive"]["score"] == 91.0
        assert alpha["comprehensive"]["source_version"] == 1
        assert alpha["retrieval"]["status"] == "queued"
        assert alpha["retrieval"]["score"] is None
        assert alpha["reasoning"]["status"] == "not_evaluated"
        assert beta["reasoning"]["status"] == "evaluated"
        assert beta["reasoning"]["score"] == 87.0
        assert beta["comprehensive"]["status"] == "failed"
        assert beta["retrieval"]["status"] == "failed"
        assert beta["data_reasoning"]["status"] == "failed"

        readonly_login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://test"},
            json={"role": "read_only", "password": READ_ONLY_PASSWORD},
        )
        assert readonly_login.status_code == 200
        assert (await client.get("/api/v1/evaluation-runs/agent-statuses")).status_code == 403


async def test_agent_status_surfaces_latest_invalid_unified_result(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        await login_operator(client)
        now = datetime.now(UTC)
        async with session_scope(app.state.session_factory) as session:
            version = await session.get(AgentVersion, fixture.agent_version_ids[0])
            assert version is not None
            run = EvaluationRun(
                name="Invalid unified result",
                status=RunStatus.PUBLISHED,
                stage=RunStage.READY_TO_PUBLISH,
                dataset_bundle_hash="a" * 64,
                profile_key="unified-v3-reasoning",
                run_scope=RunScope.SPECIALIST,
                target_ability=Ability.REASONING,
                ranking_eligible=True,
                protocol_hash="b" * 64,
                judge_config_id=fixture.judge_config_id,
                score_config_id=fixture.score_config_id,
                finished_at=now,
            )
            session.add(run)
            await session.flush()
            run_agent = RunAgent(
                run_id=run.id,
                agent_version_id=version.id,
                status=RunAgentStatus.COMPLETED,
                validity_status=ResultValidityStatus.INVALID,
                validity_reason="configuration_failure_rate_at_least_20_percent",
                configuration_failure_count=1,
                configuration_failure_rate=Decimal("20"),
            )
            session.add(run_agent)
            session.add(
                LeaderboardSnapshot(
                    run_id=run.id,
                    published_at=now,
                    rankings_json=[],
                )
            )
            run_id = run.id

        statuses = await client.get("/api/v1/evaluation-runs/agent-statuses")
        alpha = next(item for item in statuses.json() if item["agent_name"] == "Agent Alpha")
        assert alpha["reasoning"] == {
            "status": "invalid",
            "score": None,
            "run_id": str(run_id),
            "source_version": 1,
            "updated_at": alpha["reasoning"]["updated_at"],
            "message": "configuration_failure_rate_at_least_20_percent",
        }
        assert alpha["retrieval"]["status"] == "not_evaluated"

        overview = await client.get("/api/v1/leaderboards/global/overview")
        alpha_overview = next(
            item for item in overview.json()["agents"] if item["agent_name"] == "Agent Alpha"
        )
        assert alpha_overview["metrics"]["reasoning"]["status"] == "invalid"
        assert (
            alpha_overview["metrics"]["reasoning"]["message"]
            == "configuration_failure_rate_at_least_20_percent"
        )

        async with session_scope(app.state.session_factory) as session:
            version = await session.get(AgentVersion, fixture.agent_version_ids[0])
            assert version is not None
            valid_run = EvaluationRun(
                name="Newer valid unified result",
                status=RunStatus.PUBLISHED,
                stage=RunStage.READY_TO_PUBLISH,
                dataset_bundle_hash="c" * 64,
                profile_key="unified-v3-reasoning",
                run_scope=RunScope.SPECIALIST,
                target_ability=Ability.REASONING,
                ranking_eligible=True,
                protocol_hash="d" * 64,
                judge_config_id=fixture.judge_config_id,
                score_config_id=fixture.score_config_id,
                finished_at=now + timedelta(minutes=1),
            )
            session.add(valid_run)
            await session.flush()
            valid_agent = RunAgent(
                run_id=valid_run.id,
                agent_version_id=version.id,
                status=RunAgentStatus.COMPLETED,
                validity_status=ResultValidityStatus.VALID,
            )
            session.add(valid_agent)
            await session.flush()
            valid_snapshot = LeaderboardSnapshot(
                run_id=valid_run.id,
                published_at=now + timedelta(minutes=1),
                rankings_json=[],
            )
            session.add(valid_snapshot)
            await session.flush()
            session.add(
                LeaderboardResult(
                    snapshot_id=valid_snapshot.id,
                    run_agent_id=valid_agent.id,
                    agent_id=version.agent_id,
                    agent_version_id=version.id,
                    metric="reasoning",
                    score=Decimal("82"),
                    protocol_hash="e" * 64,
                    benchmark_count=1,
                )
            )

        updated_statuses = await client.get("/api/v1/evaluation-runs/agent-statuses")
        updated_alpha = next(
            item for item in updated_statuses.json() if item["agent_name"] == "Agent Alpha"
        )
        assert updated_alpha["reasoning"]["status"] == "evaluated"
        assert updated_alpha["reasoning"]["score"] == 82
        assert updated_alpha["reasoning"]["message"] is None


async def test_cancel_api_persists_command_and_worker_finalizes_queued_run(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        created = await client.post(
            "/api/v1/evaluation-runs",
            headers=mutation_headers,
            json=run_payload(fixture),
        )
        run_id = UUID(created.json()["id"])

        cancellation = await client.post(
            f"/api/v1/evaluation-runs/{run_id}/cancel",
            headers=mutation_headers,
        )
        assert cancellation.status_code == 200
        assert cancellation.json()["status"] == "queued"
        assert cancellation.json()["desired_state"] == "canceled"

        persisted = await client.get(f"/api/v1/evaluation-runs/{run_id}")
        assert persisted.status_code == 200
        assert persisted.json()["desired_state"] == "canceled"

        assert await acquire_worker_lease(app.state.engine, "worker") is True
        assert await apply_requested_cancellations(app.state.engine, "worker") == [run_id]

        finalized = await client.get(f"/api/v1/evaluation-runs/{run_id}")
        assert finalized.json()["status"] == "canceled"
        assert finalized.json()["finished_at"] is not None
        async with app.state.session_factory() as session:
            item_statuses = set(
                await session.scalars(
                    select(ExecutionItem.status)
                    .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                    .where(RunAgent.run_id == run_id)
                )
            )
        assert item_statuses == {ExecutionStatus.CANCELED}


async def test_cancellation_abandons_active_attempts_without_waiting(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        created = await client.post(
            "/api/v1/evaluation-runs",
            headers=mutation_headers,
            json=run_payload(fixture),
        )
        run_id = UUID(created.json()["id"])
        assert await acquire_worker_lease(app.state.engine, "worker") is True
        assert await claim_next_run(app.state.engine, "worker") == run_id

        async with session_scope(app.state.session_factory) as session:
            item = await session.scalar(
                select(ExecutionItem)
                .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                .where(RunAgent.run_id == run_id)
                .limit(1)
            )
            assert item is not None
            item.status = ExecutionStatus.IN_PROGRESS
            item.attempt_count = 1
            attempt = HttpAttempt(
                execution_item_id=item.id,
                attempt_no=1,
                status=AttemptStatus.IN_PROGRESS,
            )
            session.add(attempt)
            await session.flush()
            attempt_id = attempt.id

        cancellation = await client.post(
            f"/api/v1/evaluation-runs/{run_id}/cancel",
            headers=mutation_headers,
        )
        assert cancellation.status_code == 200
        assert await apply_requested_cancellations(app.state.engine, "worker") == [run_id]

        async with app.state.session_factory() as session:
            run = await session.get(EvaluationRun, run_id)
            stored_attempt = await session.get(HttpAttempt, attempt_id)
            statuses = set(
                await session.scalars(
                    select(ExecutionItem.status)
                    .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                    .where(RunAgent.run_id == run_id)
                )
            )
        assert run is not None and run.status == RunStatus.CANCELED
        assert run.active_slot is None
        assert run.claim_owner_id is None
        assert run.claimed_at is None
        assert await heartbeat_run(app.state.engine, "worker", run_id) is False
        assert stored_attempt is not None
        assert stored_attempt.status == AttemptStatus.ABANDONED
        assert stored_attempt.error_type == "run_canceled"
        assert stored_attempt.finished_at is not None
        assert statuses == {ExecutionStatus.CANCELED}


@pytest.mark.parametrize(
    "scoring_error",
    ["answer_scoring_judge_rate_limit", "citation_scoring_judge_invalid_response"],
)
async def test_retry_scoring_requeues_only_retryable_system_failure(
    tmp_path: Path, scoring_error: str
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        created = await client.post(
            "/api/v1/evaluation-runs",
            headers=mutation_headers,
            json=run_payload(fixture),
        )
        run_id = UUID(created.json()["id"])
        async with session_scope(app.state.session_factory) as session:
            run = await session.get(EvaluationRun, run_id)
            run_agent = await session.scalar(
                select(RunAgent).where(RunAgent.run_id == run_id)
            )
            assert run is not None and run_agent is not None
            run.status = RunStatus.SYSTEM_FAILED
            run.stage = RunStage.JUDGING
            run.system_error = scoring_error
            run.finished_at = datetime.now(UTC)
            original_started_at = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
            run.started_at = original_started_at
            run_agent.status = RunAgentStatus.COMPLETED

        response = await client.post(
            f"/api/v1/evaluation-runs/{run_id}/retry-scoring",
            headers=mutation_headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        assert response.json()["system_error"] is None

        assert await acquire_worker_lease(app.state.engine, "worker") is True
        assert await claim_next_run(app.state.engine, "worker") == run_id
        async with app.state.session_factory() as session:
            run = await session.get(EvaluationRun, run_id)
            run_agent = await session.scalar(
                select(RunAgent).where(RunAgent.run_id == run_id)
            )
        assert run is not None
        assert run.started_at is not None
        assert run.started_at.replace(tzinfo=UTC) == original_started_at
        assert run_agent is not None
        assert run_agent.status == RunAgentStatus.COMPLETED

        second_retry = await client.post(
            f"/api/v1/evaluation-runs/{run_id}/retry-scoring",
            headers=mutation_headers,
        )
        assert second_retry.status_code == 409
        assert second_retry.json()["detail"]["code"] == "run_scoring_not_retryable"


async def test_worker_marks_run_system_failed_after_scoring_error(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        created = await client.post(
            "/api/v1/evaluation-runs",
            headers=mutation_headers,
            json=run_payload(fixture),
        )
        run_id = UUID(created.json()["id"])

        assert await acquire_worker_lease(app.state.engine, "worker") is True
        assert await claim_next_run(app.state.engine, "worker") == run_id
        assert (
            await mark_run_system_failed(
                app.state.engine,
                "worker",
                run_id,
                "answer_scoring_system_error",
            )
            is True
        )

        async with app.state.session_factory() as session:
            run = await session.get(EvaluationRun, run_id)
        assert run is not None
        assert run.status == RunStatus.SYSTEM_FAILED
        assert run.active_slot is None
        assert run.system_error == "answer_scoring_system_error"
        assert run.finished_at is not None


async def test_worker_leases_allow_independent_owners(tmp_path: Path) -> None:
    database_path = tmp_path / "lease.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    engine = create_engine(
        Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    )
    started_at = datetime(2026, 7, 16, tzinfo=UTC)

    try:
        results = await asyncio.gather(
            acquire_worker_lease(engine, "worker-a", now=started_at),
            acquire_worker_lease(engine, "worker-b", now=started_at),
        )
        assert results == [True, True]
        assert await acquire_worker_lease(
            engine, "worker-a", now=started_at + timedelta(seconds=29)
        )
        assert await acquire_worker_lease(
            engine, "worker-c", now=started_at + timedelta(seconds=31)
        )
        assert await release_worker_lease(engine, "worker-b") is False
        assert await release_worker_lease(engine, "worker-a") is True
        assert await release_worker_lease(engine, "worker-a") is False
        assert await release_worker_lease(engine, "worker-c") is True
    finally:
        await engine.dispose()


async def test_queue_claims_three_fair_runs_with_provider_and_version_limits(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "queue.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    engine = create_engine(
        Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    )
    session_factory = create_session_factory(engine)
    fixture = await seed_run_configuration(session_factory)
    base_time = datetime(2026, 7, 16, tzinfo=UTC)

    try:
        async with session_scope(session_factory) as session:
            alpha_v1 = await session.get(AgentVersion, fixture.agent_version_ids[0])
            assert alpha_v1 is not None
            alpha_v2 = AgentVersion(
                agent_id=alpha_v1.agent_id,
                version=2,
                endpoint_url="https://agent.example/alpha-v2",
                auth_type=AuthType.NONE,
                request_template=alpha_v1.request_template,
                answer_jsonpath=alpha_v1.answer_jsonpath,
            )
            other_agent = Agent(
                name="Agent Gamma", provider="Other Provider", status=AgentStatus.ENABLED
            )
            session.add_all([alpha_v2, other_agent])
            await session.flush()
            other_version = AgentVersion(
                agent_id=other_agent.id,
                version=1,
                endpoint_url="https://agent.example/gamma",
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
            )
            session.add(other_version)
            await session.flush()

            first = await prepare_evaluation_run(
                session,
                name="First Run",
                agent_version_ids=[fixture.agent_version_ids[0]],
                dataset_version_ids=fixture.dataset_version_ids,
                judge_config_id=fixture.judge_config_id,
                score_config_id=fixture.score_config_id,
            )
            first.run.created_at = base_time
            same_version = await prepare_evaluation_run(
                session,
                name="Same Version Run",
                agent_version_ids=[fixture.agent_version_ids[0]],
                dataset_version_ids=fixture.dataset_version_ids,
                judge_config_id=fixture.judge_config_id,
                score_config_id=fixture.score_config_id,
            )
            same_version.run.created_at = base_time + timedelta(seconds=1)
            second_provider_run = await prepare_evaluation_run(
                session,
                name="Second Provider Run",
                agent_version_ids=[fixture.agent_version_ids[1]],
                dataset_version_ids=fixture.dataset_version_ids,
                judge_config_id=fixture.judge_config_id,
                score_config_id=fixture.score_config_id,
            )
            second_provider_run.run.created_at = base_time + timedelta(seconds=2)
            provider_limited = await prepare_evaluation_run(
                session,
                name="Provider Limited Run",
                agent_version_ids=[alpha_v2.id],
                dataset_version_ids=fixture.dataset_version_ids,
                judge_config_id=fixture.judge_config_id,
                score_config_id=fixture.score_config_id,
            )
            provider_limited.run.created_at = base_time + timedelta(seconds=3)
            other_provider_run = await prepare_evaluation_run(
                session,
                name="Other Provider Run",
                agent_version_ids=[other_version.id],
                dataset_version_ids=fixture.dataset_version_ids,
                judge_config_id=fixture.judge_config_id,
                score_config_id=fixture.score_config_id,
            )
            other_provider_run.run.created_at = base_time + timedelta(seconds=4)
            first_id = first.run.id
            same_version_id = same_version.run.id
            second_provider_id = second_provider_run.run.id
            provider_limited_id = provider_limited.run.id
            other_provider_id = other_provider_run.run.id

        assert await claim_next_run(engine, "not-owner", now=base_time) is None
        assert await acquire_worker_lease(engine, "worker", now=base_time) is True
        assert await claim_next_run(engine, "worker", now=base_time) == first_id
        assert await claim_next_run(engine, "worker", now=base_time) == second_provider_id
        assert await claim_next_run(engine, "worker", now=base_time) == other_provider_id
        assert await claim_next_run(engine, "worker", now=base_time) is None
        assert await heartbeat_run(engine, "worker", second_provider_id, now=base_time) is True

        async with session_factory() as session:
            claimed_runs = list(
                await session.scalars(
                    select(EvaluationRun).where(
                        EvaluationRun.id.in_([first_id, second_provider_id, other_provider_id])
                    )
                )
            )
            same_version_run = await session.get(EvaluationRun, same_version_id)
            provider_limited_run = await session.get(EvaluationRun, provider_limited_id)
            run_agent_statuses = set(
                await session.scalars(select(RunAgent.status).where(RunAgent.run_id == first_id))
            )
        assert {run.status for run in claimed_runs} == {RunStatus.RUNNING}
        assert {run.active_slot for run in claimed_runs} == {1, 2, 3}
        assert same_version_run is not None and same_version_run.status == RunStatus.QUEUED
        assert provider_limited_run is not None
        assert provider_limited_run.status == RunStatus.QUEUED
        assert run_agent_statuses == {RunAgentStatus.RUNNING}

        async with session_factory() as session:
            queued = await session.get(EvaluationRun, same_version_id)
            assert queued is not None
            queued.status = RunStatus.RUNNING
            queued.active_slot = 1
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

        async with session_scope(session_factory) as session:
            first_run = await session.get(EvaluationRun, first_id)
            assert first_run is not None
            first_run.status = RunStatus.COMPLETED
            first_run.active_slot = None
            first_run.claim_owner_id = None
            first_run.claimed_at = None

        assert await claim_next_run(engine, "worker", now=base_time) == same_version_id
    finally:
        await engine.dispose()


async def test_stale_run_recovery_abandons_attempts_without_repeating_completed_items(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "recovery.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    artifact_store = create_artifact_store(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    fixture = await seed_run_configuration(session_factory)
    started_at = datetime(2026, 7, 16, tzinfo=UTC)

    try:
        async with session_scope(session_factory) as session:
            prepared = await prepare_evaluation_run(
                session,
                name="Recoverable Run",
                agent_version_ids=fixture.agent_version_ids,
                dataset_version_ids=fixture.dataset_version_ids,
                judge_config_id=fixture.judge_config_id,
                score_config_id=fixture.score_config_id,
            )
            run_id = prepared.run.id

        assert await acquire_worker_lease(engine, "old-worker", now=started_at) is True
        assert await claim_next_run(engine, "old-worker", now=started_at) == run_id

        async with session_scope(session_factory) as session:
            run = await session.get(EvaluationRun, run_id)
            run_agent_id = await session.scalar(
                select(RunAgent.id).where(RunAgent.run_id == run_id).order_by(RunAgent.id)
            )
            assert run is not None and run_agent_id is not None
            run.active_slot = 2
            items = list(
                await session.scalars(
                    select(ExecutionItem)
                    .where(ExecutionItem.run_agent_id == run_agent_id)
                    .order_by(ExecutionItem.id)
                )
            )
            first_attempt_item, second_attempt_item, completed_item = items[:3]
            first_attempt_item.status = ExecutionStatus.IN_PROGRESS
            first_attempt_item.attempt_count = 1
            second_attempt_item.status = ExecutionStatus.IN_PROGRESS
            second_attempt_item.attempt_count = 2
            completed_item.status = ExecutionStatus.ANSWERED
            completed_item.attempt_count = 1
            session.add_all(
                [
                    HttpAttempt(
                        execution_item_id=first_attempt_item.id,
                        attempt_no=1,
                        status=AttemptStatus.IN_PROGRESS,
                        started_at=started_at,
                    ),
                    HttpAttempt(
                        execution_item_id=second_attempt_item.id,
                        attempt_no=1,
                        status=AttemptStatus.FAILED,
                        started_at=started_at - timedelta(seconds=2),
                        finished_at=started_at - timedelta(seconds=1),
                        error_type="timeout",
                    ),
                    HttpAttempt(
                        execution_item_id=second_attempt_item.id,
                        attempt_no=2,
                        status=AttemptStatus.IN_PROGRESS,
                        started_at=started_at,
                    ),
                ]
            )
            first_item_id = first_attempt_item.id
            second_item_id = second_attempt_item.id
            completed_item_id = completed_item.id

        takeover_time = started_at + timedelta(seconds=60)
        assert await acquire_worker_lease(engine, "new-worker", now=takeover_time) is True
        assert await recover_stale_run(engine, "new-worker", now=takeover_time) is None
        recovery_time = started_at + timedelta(seconds=61)
        recovery = await recover_stale_run(engine, "new-worker", now=recovery_time)

        assert recovery is not None
        assert recovery.run_id == run_id
        assert recovery.abandoned_attempts == 2
        assert recovery.reset_items == 1
        assert recovery.failed_items == 1
        assert recovery.canceled is False
        assert await recover_stale_run(engine, "new-worker", now=recovery_time) is None

        # Both process leases remain valid; the transferred claim alone fences the old owner.
        assert await acquire_worker_lease(engine, "old-worker", now=recovery_time) is True

        old_writer = ResultWriter(
            session_factory,
            settings,
            run_id,
            "old-worker",
            artifact_store,
        )
        old_writer_task = asyncio.create_task(old_writer.run())
        try:
            assert await old_writer.start_attempt(first_item_id, 2) is None
        finally:
            await old_writer.stop()
            await old_writer_task

        async with session_scope(session_factory) as session:
            run = await session.get(EvaluationRun, run_id)
            assert run is not None
            assert run.claim_owner_id == "new-worker"
            assert run.claimed_at is not None
            run.status = RunStatus.SCORING
            run.stage = RunStage.JUDGING

        scoring_writer = ScoringWriter(
            session_factory,
            settings,
            run_id,
            "old-worker",
            artifact_store,
        )
        scoring_task = asyncio.create_task(scoring_writer.run())
        try:
            assert (
                await scoring_writer.write(
                    ScoreResult(first_item_id, 0, "fenced", False, False)
                )
                is False
            )
        finally:
            await scoring_writer.stop()
            await scoring_task

        citation_scoring_writer = CitationScoringWriter(
            session_factory,
            settings,
            run_id,
            "old-worker",
            artifact_store,
        )
        citation_scoring_task = asyncio.create_task(citation_scoring_writer.run())
        try:
            assert (
                await citation_scoring_writer.write(
                    CitationScoreResult(uuid4(), 0, 0, "fenced")
                )
                is False
            )
        finally:
            await citation_scoring_writer.stop()
            await citation_scoring_task

        async with session_scope(session_factory) as session:
            run = await session.get(EvaluationRun, run_id)
            assert run is not None
            run.stage = RunStage.CITATION_FETCH
        citation_writer = CitationWriter(
            session_factory,
            settings,
            run_id,
            "old-worker",
            artifact_store,
        )
        citation_task = asyncio.create_task(citation_writer.run())
        try:
            assert await citation_writer.start(uuid4()) is False
            assert await citation_writer.finish_stage() is False
        finally:
            await citation_writer.stop()
            await citation_task

        async with session_scope(session_factory) as session:
            run = await session.get(EvaluationRun, run_id)
            assert run is not None
            run.stage = RunStage.AGGREGATING
        assert (
            await execute_aggregation_and_publish(
                session_factory, run_id, owner_id="old-worker"
            )
            is False
        )

        assert (
            await heartbeat_run(
                engine, "new-worker", run_id, now=recovery_time + timedelta(seconds=10)
            )
            is True
        )
        assert (
            await heartbeat_run(
                engine, "old-worker", run_id, now=recovery_time + timedelta(seconds=10)
            )
            is False
        )

        async with session_factory() as session:
            first_item = await session.get(ExecutionItem, first_item_id)
            second_item = await session.get(ExecutionItem, second_item_id)
            completed = await session.get(ExecutionItem, completed_item_id)
            run_agent = await session.get(RunAgent, run_agent_id)
            attempt_statuses = list(
                await session.scalars(
                    select(HttpAttempt.status)
                    .join(ExecutionItem, ExecutionItem.id == HttpAttempt.execution_item_id)
                    .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                    .where(RunAgent.run_id == run_id)
                    .order_by(HttpAttempt.attempt_no)
                )
            )

        assert first_item is not None and first_item.status == ExecutionStatus.PENDING
        assert first_item.error_type == "worker_restart_retry"
        assert second_item is not None and second_item.status == ExecutionStatus.FAILED
        assert second_item.error_type == "worker_restart_after_retry"
        assert completed is not None and completed.status == ExecutionStatus.ANSWERED
        assert run_agent is not None
        assert run_agent.completed == 1
        assert run_agent.failed == 1
        assert run_agent.retried == 1
        assert run_agent.status == RunAgentStatus.RUNNING
        assert attempt_statuses.count(AttemptStatus.ABANDONED) == 2
        assert attempt_statuses.count(AttemptStatus.FAILED) == 1

        async with engine.begin() as connection:
            await connection.execute(
                ExecutionItem.__table__.update()
                .where(ExecutionItem.id == first_item_id)
                .values(status=ExecutionStatus.ANSWERED)
            )
            await _recompute_run_agent_counts(connection, run_id)
        async with session_factory() as session:
            recovered_agent_status = await session.scalar(
                select(RunAgent.status).where(RunAgent.id == run_agent_id)
            )
        assert recovered_agent_status == RunAgentStatus.COMPLETED
    finally:
        await engine.dispose()


async def test_stale_canceled_run_abandons_inflight_attempt_then_releases_slot(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "canceled-recovery.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    engine = create_engine(
        Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    )
    session_factory = create_session_factory(engine)
    fixture = await seed_run_configuration(session_factory)
    started_at = datetime(2026, 7, 16, tzinfo=UTC)

    try:
        async with session_scope(session_factory) as session:
            prepared = await prepare_evaluation_run(
                session,
                name="Canceled Crash Run",
                agent_version_ids=fixture.agent_version_ids,
                dataset_version_ids=fixture.dataset_version_ids,
                judge_config_id=fixture.judge_config_id,
                score_config_id=fixture.score_config_id,
            )
            run_id = prepared.run.id
        assert await acquire_worker_lease(engine, "old-worker", now=started_at) is True
        assert await claim_next_run(engine, "old-worker", now=started_at) == run_id

        async with session_scope(session_factory) as session:
            run = await session.get(EvaluationRun, run_id)
            item = await session.scalar(
                select(ExecutionItem)
                .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                .where(RunAgent.run_id == run_id)
            )
            assert run is not None and item is not None
            run.desired_state = DesiredRunState.CANCELED
            item.status = ExecutionStatus.IN_PROGRESS
            item.attempt_count = 1
            session.add(
                HttpAttempt(
                    execution_item_id=item.id,
                    attempt_no=1,
                    status=AttemptStatus.IN_PROGRESS,
                    started_at=started_at,
                )
            )

        recovery_time = started_at + timedelta(seconds=61)
        assert await acquire_worker_lease(engine, "new-worker", now=recovery_time) is True
        assert await apply_requested_cancellations(engine, "new-worker", now=recovery_time) == [
            run_id
        ]

        recovery = await recover_stale_run(engine, "new-worker", now=recovery_time)
        assert recovery is None

        async with session_factory() as session:
            run = await session.get(EvaluationRun, run_id)
            statuses = set(
                await session.scalars(
                    select(ExecutionItem.status)
                    .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                    .where(RunAgent.run_id == run_id)
                )
            )
            attempt_status = await session.scalar(select(HttpAttempt.status))
        assert run is not None and run.status == RunStatus.CANCELED
        assert run.active_slot is None
        assert statuses == {ExecutionStatus.CANCELED}
        assert attempt_status == AttemptStatus.ABANDONED
    finally:
        await engine.dispose()
