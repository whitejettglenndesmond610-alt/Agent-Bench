import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import StatementError

from agent_eval.agents.inference import InferenceOutcome
from agent_eval.core.artifact_store import create_artifact_store
from agent_eval.core.settings import Settings
from agent_eval.core.storage import ensure_storage
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import (
    Ability,
    AgentStatus,
    ArtifactKind,
    AuthType,
    CitationFetchStatus,
    DatasetStatus,
    ExecutionStatus,
    Language,
    QuestionType,
    RunAgentStatus,
    RunStage,
    RunStatus,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    ArtifactIndex,
    Benchmark,
    CitationRecord,
    DatasetVersion,
    EvaluationRun,
    ExecutionItem,
    HttpAttempt,
    JudgeConfig,
    LeaderboardSnapshot,
    Notification,
    Question,
    RunAgent,
    ScoreConfig,
    ScoreRecord,
)
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.leaderboards import publisher as leaderboard_publisher
from agent_eval.leaderboards.publisher import execute_aggregation_and_publish
from agent_eval.runs.queue import acquire_worker_lease, claim_next_run
from agent_eval.runs.service import prepare_evaluation_run
from agent_eval.worker.citation_scoring import execute_citation_scoring
from agent_eval.worker.citations import execute_citation_fetch
from agent_eval.worker.executor import AgentCircuitBreaker, WorkItem, execute_run_inference
from agent_eval.worker.scoring import execute_answer_scoring


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


async def no_sleep(_: float) -> None:
    return None


def failed_outcome(error_type: str) -> InferenceOutcome:
    return InferenceOutcome(
        success=False,
        canceled=False,
        answer=None,
        citations=[],
        response_content=None,
        latency_ms=1,
        attempt_count=1,
        error_type=error_type,
    )


async def test_agent_circuit_breaker_limits_deterministic_failures() -> None:
    input_circuit = AgentCircuitBreaker()
    await input_circuit.record(failed_outcome("agent_input_limit"), 400)
    long_item = WorkItem(uuid4(), uuid4(), uuid4(), 0, "x" * 400)
    short_item = WorkItem(uuid4(), uuid4(), uuid4(), 0, "x" * 399)
    assert await input_circuit.skip_reason(long_item) == "agent_circuit_input_limit"
    assert await input_circuit.skip_reason(short_item) is None

    auth_circuit = AgentCircuitBreaker()
    await auth_circuit.record(failed_outcome("agent_authentication"), 10)
    assert await auth_circuit.skip_reason(short_item) == "agent_circuit_authentication"

    schema_circuit = AgentCircuitBreaker()
    await schema_circuit.record(failed_outcome("agent_response_schema"), 10)
    assert await schema_circuit.skip_reason(short_item) is None
    await schema_circuit.record(failed_outcome("agent_response_schema"), 10)
    assert await schema_circuit.skip_reason(short_item) == "agent_circuit_response_schema"


async def test_executor_enforces_concurrency_and_persists_attempts_and_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "executor.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        data_dir=tmp_path,
        deepseek_api_key="fake-key",
    )
    ensure_storage(settings)
    artifact_store = create_artifact_store(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_scope(session_factory) as session:
            agent = Agent(name="Executor Agent", provider="Provider", status=AgentStatus.ENABLED)
            session.add(agent)
            await session.flush()
            version = AgentVersion(
                agent_id=agent.id,
                version=1,
                endpoint_url="https://agent.example/run",
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
                citations_jsonpath="$.citations",
                concurrency=2,
            )
            benchmark = Benchmark(
                name="Executor Dataset",
                ability=Ability.RETRIEVAL,
                language=Language.EN,
                source="https://example.test",
            )
            session.add_all([version, benchmark])
            await session.flush()
            dataset = DatasetVersion(
                benchmark_id=benchmark.id,
                version=1,
                source_version="v1",
                content_hash="c" * 64,
                question_count=4,
                status=DatasetStatus.FROZEN,
                frozen_at=datetime.now(UTC),
            )
            judge = JudgeConfig(provider="deepseek", model="deepseek-chat", prompt_version="v1")
            score = ScoreConfig(name="Default")
            session.add_all([dataset, judge, score])
            await session.flush()
            session.add_all(
                [
                    Question(
                        dataset_version_id=dataset.id,
                        external_id=f"q-{index}",
                        type=(
                            QuestionType.OPEN_ENDED
                            if index == 3
                            else QuestionType.SHORT_ANSWER
                        ),
                        prompt=f"Question {index}",
                        reference_answer=f"Answer {index}",
                        requires_citations=index != 0,
                    )
                    for index in range(4)
                ]
            )
            await session.flush()
            prepared = await prepare_evaluation_run(
                session,
                name="Executor Run",
                agent_version_ids=[version.id],
                dataset_version_ids=[dataset.id],
                judge_config_id=judge.id,
                score_config_id=score.id,
            )
            run_id = prepared.run.id

        assert await acquire_worker_lease(engine, "worker") is True
        assert await claim_next_run(engine, "worker") == run_id

        active_requests = 0
        max_active_requests = 0
        call_count = 0
        question_calls: dict[str, int] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active_requests, max_active_requests, call_count
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)
            call_count += 1
            try:
                await asyncio.sleep(0.01)
                request_body = json.loads(request.content)
                question = next(
                    value
                    for value in (f"Question {index}" for index in range(4))
                    if value in request_body["query"]
                )
                question_calls[question] = question_calls.get(question, 0) + 1
                if question == "Question 0" and question_calls[question] == 1:
                    return httpx.Response(429, headers={"Retry-After": "0"}, json={})
                if question == "Question 2":
                    return httpx.Response(503, json={})
                return httpx.Response(
                    200,
                    json={
                        "answer": "Agent answer",
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
                        "citations": (
                            []
                            if "Question 1" in request_body["query"]
                            else [f"https://example.com/{index}" for index in range(4)]
                        ),
                    },
                )
            finally:
                active_requests -= 1

        completed = await execute_run_inference(
            session_factory,
            settings,
            run_id,
            artifact_store=artifact_store,
            token_cipher=None,
            owner_id="worker",
            transport_factory=lambda _: httpx.MockTransport(handler),
            sleep=no_sleep,
        )

        assert completed is True
        assert max_active_requests == 2
        assert call_count == 6
        async with session_factory() as session:
            run = await session.get(EvaluationRun, run_id)
            run_agent = await session.scalar(select(RunAgent).where(RunAgent.run_id == run_id))
            item_statuses = set(await session.scalars(select(ExecutionItem.status)))
            attempt_count = await session.scalar(select(func.count()).select_from(HttpAttempt))
            attempts = list(await session.scalars(select(HttpAttempt)))
            artifact_count = await session.scalar(select(func.count()).select_from(ArtifactIndex))
            citation_count = await session.scalar(select(func.count()).select_from(CitationRecord))
            artifacts = list(await session.scalars(select(ArtifactIndex)))

        assert run is not None and run.status == RunStatus.SCORING
        assert run.stage == RunStage.JUDGING
        assert run.active_slot == 1
        assert run_agent is not None and run_agent.status == RunAgentStatus.COMPLETED
        assert run_agent.completed == 3
        assert run_agent.failed == 1
        assert run_agent.retried == 2
        assert item_statuses == {ExecutionStatus.ANSWERED, ExecutionStatus.FAILED}
        assert attempt_count == 6
        assert sum(attempt.input_tokens or 0 for attempt in attempts) == 30
        assert sum(attempt.output_tokens or 0 for attempt in attempts) == 15
        assert sum(attempt.total_tokens or 0 for attempt in attempts) == 45
        assert sum(attempt.total_tokens is not None for attempt in attempts) == 3
        assert artifact_count == 4
        assert citation_count == 3
        for artifact in artifacts:
            content = (settings.artifact_dir / artifact.relative_path).read_bytes()
            assert hashlib.sha256(content).hexdigest() == artifact.sha256
            assert len(content) == artifact.size_bytes

        judge_calls = 0

        async def judge_handler(request: httpx.Request) -> httpx.Response:
            nonlocal judge_calls
            judge_calls += 1
            assert request.headers["Authorization"] == "Bearer fake-key"
            request_body = json.loads(request.content)
            user_data = json.loads(request_body["messages"][1]["content"])
            verdict = (
                {"rating": 5, "reason": "Complete"}
                if user_data["question"] == "Question 3"
                else {"verdict": "correct", "reason": "Equivalent"}
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(verdict)
                            }
                        }
                    ]
                },
            )

        scoring_complete = await execute_answer_scoring(
            session_factory,
            settings,
            run_id,
            artifact_store=artifact_store,
            owner_id="worker",
            transport=httpx.MockTransport(judge_handler),
            sleep=no_sleep,
        )
        assert scoring_complete is True
        assert judge_calls == 3

        async with session_factory() as session:
            run = await session.get(EvaluationRun, run_id)
            scores = list(await session.scalars(select(ScoreRecord)))
            judge_artifacts = list(
                await session.scalars(
                    select(ArtifactIndex).where(ArtifactIndex.kind == ArtifactKind.JUDGE_RESPONSE)
                )
            )
            scored_item_statuses = set(await session.scalars(select(ExecutionItem.status)))
        assert len(scores) == 4
        assert {int(score.answer_score) for score in scores} == {0, 100}
        assert {score.judge_reason for score in scores} >= {"Equivalent", "Complete"}
        assert len(judge_artifacts) == 3
        assert scored_item_statuses == {
            ExecutionStatus.ANSWERED,
            ExecutionStatus.FAILED,
            ExecutionStatus.SCORED,
        }
        assert run is not None and run.stage == RunStage.CITATION_FETCH

        active_fetches = 0
        max_active_fetches = 0

        async def citation_resolver(_: str, __: int) -> list[str]:
            return ["93.184.216.34"]

        async def citation_handler(request: httpx.Request) -> httpx.Response:
            nonlocal active_fetches, max_active_fetches
            active_fetches += 1
            max_active_fetches = max(max_active_fetches, active_fetches)
            try:
                await asyncio.sleep(0.01)
                if request.url.path.endswith("/0"):
                    return httpx.Response(
                        200,
                        headers={"Content-Type": "text/html"},
                        content=b"<html><body><script>hidden only</script></body></html>",
                    )
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    content=b"<html><body>Supporting source</body></html>",
                )
            finally:
                active_fetches -= 1

        citation_fetch_complete = await execute_citation_fetch(
            session_factory,
            settings,
            run_id,
            artifact_store=artifact_store,
            owner_id="worker",
            resolver=citation_resolver,
            transport=httpx.MockTransport(citation_handler),
        )
        assert citation_fetch_complete is True
        assert max_active_fetches == settings.citation_domain_concurrency

        async with session_factory() as session:
            run = await session.get(EvaluationRun, run_id)
            citations = list(await session.scalars(select(CitationRecord)))
            citation_artifacts = list(
                await session.scalars(
                    select(ArtifactIndex).where(
                        ArtifactIndex.kind == ArtifactKind.CITATION_CONTENT
                    )
                )
            )
        assert run is not None and run.stage == RunStage.JUDGING
        assert len(citations) == 3
        assert {citation.fetch_status for citation in citations} == {
            CitationFetchStatus.FETCHED,
        }
        assert {citation.http_status for citation in citations} == {200}
        assert all(citation.artifact_id is not None for citation in citations)
        assert len(citation_artifacts) == 3
        for artifact in citation_artifacts:
            content = (settings.artifact_dir / artifact.relative_path).read_bytes()
            assert hashlib.sha256(content).hexdigest() == artifact.sha256
            assert len(content) == artifact.size_bytes

        support_judge_calls = 0

        async def support_judge_handler(request: httpx.Request) -> httpx.Response:
            nonlocal support_judge_calls
            support_judge_calls += 1
            request_body = json.loads(request.content)
            user_data = json.loads(request_body["messages"][1]["content"])
            assert "Supporting source" in user_data["source_text"]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "support_score": 80,
                                        "reason": "Supports the answer",
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        citation_scoring_complete = await execute_citation_scoring(
            session_factory,
            settings,
            run_id,
            artifact_store=artifact_store,
            owner_id="worker",
            transport=httpx.MockTransport(support_judge_handler),
            sleep=no_sleep,
        )
        assert citation_scoring_complete is True
        assert support_judge_calls == 2

        async with session_factory() as session:
            run = await session.get(EvaluationRun, run_id)
            citations = list(await session.scalars(select(CitationRecord)))
            scores = list(await session.scalars(select(ScoreRecord)))
            item_statuses = set(await session.scalars(select(ExecutionItem.status)))
        assert run is not None and run.stage == RunStage.AGGREGATING
        assert {citation.fetch_status for citation in citations} == {
            CitationFetchStatus.FETCHED,
            CitationFetchStatus.UNSUPPORTED,
        }
        assert {int(citation.authenticity_score or 0) for citation in citations} == {0, 100}
        assert {int(citation.support_score or 0) for citation in citations} == {0, 80}
        assert sum(citation.judge_artifact_id is not None for citation in citations) == 2
        assert {int(score.citation_score or 0) for score in scores} == {0, 60}
        assert {int(score.final_score) for score in scores} == {0, 70, 88, 100}
        assert item_statuses == {ExecutionStatus.FAILED, ExecutionStatus.SCORED}

        async with session_scope(session_factory) as session:
            old_run = EvaluationRun(
                name="Previous Published Run",
                status=RunStatus.PUBLISHED,
                stage=RunStage.READY_TO_PUBLISH,
                dataset_bundle_hash="d" * 64,
                judge_config_id=judge.id,
                score_config_id=score.id,
            )
            session.add(old_run)
            await session.flush()
            old_snapshot = LeaderboardSnapshot(
                run_id=old_run.id,
                rankings_json=[],
                is_current=True,
            )
            session.add(old_snapshot)
            await session.flush()
            old_snapshot_id = old_snapshot.id

        original_build_rankings = leaderboard_publisher.build_rankings
        monkeypatch.setattr(
            leaderboard_publisher,
            "build_rankings",
            lambda _: [{"not_serializable": object()}],
        )
        with pytest.raises(StatementError):
            await execute_aggregation_and_publish(
                session_factory, run_id, owner_id="worker"
            )

        async with session_factory() as session:
            run = await session.get(EvaluationRun, run_id)
            old_snapshot = await session.get(LeaderboardSnapshot, old_snapshot_id)
            failed_snapshot = await session.scalar(
                select(LeaderboardSnapshot).where(LeaderboardSnapshot.run_id == run_id)
            )
        assert run is not None and run.status == RunStatus.SCORING
        assert run.active_slot == 1
        assert old_snapshot is not None and old_snapshot.is_current is True
        assert failed_snapshot is None

        monkeypatch.setattr(leaderboard_publisher, "build_rankings", original_build_rankings)
        assert (
            await execute_aggregation_and_publish(
                session_factory, run_id, owner_id="worker"
            )
            is True
        )
        assert (
            await execute_aggregation_and_publish(
                session_factory, run_id, owner_id="worker"
            )
            is False
        )

        async with session_factory() as session:
            run = await session.get(EvaluationRun, run_id)
            snapshots = list(await session.scalars(select(LeaderboardSnapshot)))
            notifications = list(await session.scalars(select(Notification)))
        assert run is not None and run.status == RunStatus.PUBLISHED
        assert run.stage == RunStage.READY_TO_PUBLISH
        assert run.active_slot is None
        assert run.finished_at is not None
        assert len(snapshots) == 2
        current = next(snapshot for snapshot in snapshots if snapshot.is_current)
        assert current.run_id == old_snapshot.run_id
        published = next(snapshot for snapshot in snapshots if snapshot.run_id == run_id)
        assert published.is_current is False
        assert len(published.rankings_json) == 1
        ranking = published.rankings_json[0]
        assert ranking["rank"] == 1
        assert ranking["overall_score"] == 64.5
        assert ranking["ability_scores"] == {"retrieval": 64.5}
        assert ranking["language_scores"] == {"zh": 0.0, "en": 64.5}
        assert ranking["answer_quality"] == 75.0
        assert ranking["citation_quality"] == 20.0
        assert ranking["success_rate"] == 75.0
        assert ranking["anomaly_rate"] == 25.0
        assert ranking["http_call_count"] == 6
        assert ranking["total_input_tokens"] == 30
        assert ranking["total_output_tokens"] == 15
        assert ranking["total_tokens"] == 45
        assert ranking["token_usage_available_count"] == 3
        assert ranking["token_usage_missing_count"] == 3
        latency_samples = sorted(
            attempt.latency_ms for attempt in attempts if attempt.latency_ms is not None
        )
        assert ranking["latency_p50_ms"] == latency_samples[2]
        assert ranking["latency_p95_ms"] == latency_samples[5]
        assert ranking["latency_sample_count"] == 6
        assert len(notifications) == 1

        assert (
            await execute_run_inference(
                session_factory,
                settings,
                run_id,
                artifact_store=artifact_store,
                token_cipher=None,
                owner_id="worker",
                transport_factory=lambda _: httpx.MockTransport(handler),
                sleep=no_sleep,
            )
            is False
        )
        assert (
            await execute_answer_scoring(
                session_factory,
                settings,
                run_id,
                artifact_store=artifact_store,
                owner_id="worker",
            )
            is False
        )
        async with session_factory() as session:
            unchanged_run = await session.get(EvaluationRun, run_id)
        assert unchanged_run is not None
        assert unchanged_run.status == RunStatus.PUBLISHED
        assert unchanged_run.stage == RunStage.READY_TO_PUBLISH
    finally:
        await engine.dispose()
