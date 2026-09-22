import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from agent_eval.core.artifact_store import create_artifact_store
from agent_eval.core.settings import Settings
from agent_eval.core.storage import ensure_storage
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import (
    Ability,
    AgentStatus,
    ArtifactKind,
    AuthType,
    DatasetStatus,
    ExecutionStatus,
    Language,
    QuestionType,
    RunStage,
    RunStatus,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    ArtifactIndex,
    Benchmark,
    DatasetVersion,
    EvaluationRun,
    ExecutionItem,
    JudgeConfig,
    Question,
    ScoreConfig,
    ScoreRecord,
)
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.judges.deepseek import (
    RUBRIC_DIMENSIONS,
    RUBRIC_PROMPT_VERSION,
    JudgeError,
    judge_rubric_answer,
)
from agent_eval.quality.calibration import cohens_kappa, summarize_calibration
from agent_eval.runs.evidence import get_run_evidence_detail
from agent_eval.runs.queue import acquire_worker_lease, claim_next_run
from agent_eval.runs.service import prepare_evaluation_run
from agent_eval.worker.scoring import execute_answer_scoring


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


async def no_sleep(_: float) -> None:
    return None


def rubric_response(
    verdicts: dict[str, int], model: str | None = "deepseek-v4-flash-20260901"
) -> httpx.Response:
    payload: dict[str, object] = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "dimensions": {
                                key: {"verdict": verdict, "rationale": f"{key} rationale"}
                                for key, verdict in verdicts.items()
                            }
                        }
                    )
                }
            }
        ]
    }
    if model is not None:
        payload["model"] = model
    return httpx.Response(200, json=payload)


ALL_DIMENSION_VERDICTS = {
    "correctness": 1,
    "completeness": 1,
    "grounding": 0,
    "clarity": 1,
}


async def test_rubric_judge_weights_dimensions_into_score() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["temperature"] == 0
        system_prompt = body["messages"][0]["content"]
        assert "correctness" in system_prompt and "weight 0.4" in system_prompt
        return rubric_response(ALL_DIMENSION_VERDICTS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await judge_rubric_answer(
            client,
            api_url="https://api.deepseek.test/chat/completions",
            api_key="fake-key",
            model="deepseek-v4-flash",
            question="Question",
            reference_answer="Reference",
            candidate_answer="Candidate",
            timeout_seconds=10,
            sleep=no_sleep,
        )

    assert result.score == 80
    assert result.model_returned == "deepseek-v4-flash-20260901"
    assert [dimension.key for dimension in result.dimensions] == [
        dimension.key for dimension in RUBRIC_DIMENSIONS
    ]
    verdicts = {dimension.key: dimension.verdict for dimension in result.dimensions}
    assert verdicts == ALL_DIMENSION_VERDICTS
    assert "正确性" in result.reason and "完整性" in result.reason
    assert b"fake-key" not in result.raw_response


@pytest.mark.parametrize(
    ("verdicts", "expected_score"),
    [
        ({"correctness": 1, "completeness": 1, "grounding": 1, "clarity": 1}, 100),
        ({"correctness": 1, "completeness": 0, "grounding": 0, "clarity": 0}, 40),
        ({"correctness": 0, "completeness": 1, "grounding": 1, "clarity": 0}, 50),
        ({"correctness": 0, "completeness": 0, "grounding": 0, "clarity": 0}, 0),
    ],
)
async def test_rubric_judge_score_follows_dimension_weights(
    verdicts: dict[str, int], expected_score: int
) -> None:
    transport = httpx.MockTransport(lambda _: rubric_response(verdicts, model=None))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await judge_rubric_answer(
            client,
            api_url="https://api.deepseek.test/chat/completions",
            api_key="fake-key",
            model="deepseek-v4-flash",
            question="Question",
            reference_answer="Reference",
            candidate_answer="Candidate",
            timeout_seconds=10,
            sleep=no_sleep,
        )
    assert result.score == expected_score
    assert result.model_returned is None


async def test_rubric_judge_retries_once_when_a_dimension_is_missing() -> None:
    calls: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            incomplete = {key: 1 for key in ALL_DIMENSION_VERDICTS if key != "grounding"}
            return rubric_response(incomplete)
        return rubric_response(ALL_DIMENSION_VERDICTS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await judge_rubric_answer(
            client,
            api_url="https://api.deepseek.test/chat/completions",
            api_key="fake-key",
            model="deepseek-v4-flash",
            question="Question",
            reference_answer="Reference",
            candidate_answer="Candidate",
            timeout_seconds=10,
            sleep=no_sleep,
        )

    assert result.score == 80
    assert result.attempt_count == 2
    assert len(calls[1]["messages"]) == 3
    repair_instruction = calls[1]["messages"][2]["content"]
    assert "dimensions" in repair_instruction


async def test_rubric_judge_rejects_repeated_invalid_output() -> None:
    invalid = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})
    )
    async with httpx.AsyncClient(transport=invalid) as client:
        with pytest.raises(JudgeError, match="judge_invalid_response"):
            await judge_rubric_answer(
                client,
                api_url="https://api.deepseek.test/chat/completions",
                api_key="fake-key",
                model="deepseek-v4-flash",
                question="Question",
                reference_answer="Reference",
                candidate_answer="Candidate",
                timeout_seconds=10,
                sleep=no_sleep,
            )

    missing = httpx.MockTransport(
        lambda _: rubric_response({key: 1 for key in ALL_DIMENSION_VERDICTS if key != "clarity"})
    )
    async with httpx.AsyncClient(transport=missing) as client:
        with pytest.raises(JudgeError, match="judge_invalid_response"):
            await judge_rubric_answer(
                client,
                api_url="https://api.deepseek.test/chat/completions",
                api_key="fake-key",
                model="deepseek-v4-flash",
                question="Question",
                reference_answer="Reference",
                candidate_answer="Candidate",
                timeout_seconds=10,
                sleep=no_sleep,
            )


def test_cohens_kappa_matches_hand_computed_contingency_table() -> None:
    # judge calls: pass, pass, fail, fail; human: agree, disagree, agree, agree
    # human calls: pass, fail, fail, fail -> pp=1, pf=1, fp=0, ff=2
    # observed = 3/4, expected = 0.5*0.25 + 0.5*0.75 = 0.5 -> kappa = 0.5
    kappa = cohens_kappa([True, True, False, False], [True, False, True, True])
    assert kappa == pytest.approx(0.5)

    assert cohens_kappa([True, False], [True, True]) == pytest.approx(1.0)
    assert cohens_kappa([], []) is None
    assert cohens_kappa([True, True], [True, True]) is None
    with pytest.raises(ValueError, match="same length"):
        cohens_kappa([True], [])


def test_summarize_calibration_flags_small_samples() -> None:
    small = summarize_calibration([(100.0, True), (0.0, False), (100.0, True)])
    assert small.labeled == 3
    assert small.agree == 2
    assert small.disagree == 1
    assert small.agreement == pytest.approx(2 / 3)
    assert small.insufficient_sample is True

    large = summarize_calibration([(100.0, True)] * 30)
    assert large.insufficient_sample is False
    assert large.agreement == pytest.approx(1.0)


async def test_rubric_scoring_persists_judge_version_and_dimension_details(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "rubric-scoring.db"
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
    owner_id = "worker"

    try:
        async with session_scope(session_factory) as session:
            agent = Agent(name="Rubric Agent", provider="Provider", status=AgentStatus.ENABLED)
            session.add(agent)
            await session.flush()
            version = AgentVersion(
                agent_id=agent.id,
                version=1,
                endpoint_url="https://agent.example/run",
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
            )
            benchmark = Benchmark(
                name="Rubric Benchmark",
                ability=Ability.REASONING,
                language=Language.ZH,
                source="https://example.test",
            )
            session.add_all([version, benchmark])
            await session.flush()
            dataset = DatasetVersion(
                benchmark_id=benchmark.id,
                version=1,
                source_version="v1",
                content_hash="d" * 64,
                question_count=1,
                status=DatasetStatus.FROZEN,
                frozen_at=datetime.now(UTC),
            )
            judge = JudgeConfig(
                provider="deepseek",
                model="deepseek-v4-flash",
                prompt_version=RUBRIC_PROMPT_VERSION,
            )
            score_config = ScoreConfig(name="Rubric Score")
            session.add_all([dataset, judge, score_config])
            await session.flush()
            session.add(
                Question(
                    dataset_version_id=dataset.id,
                    external_id="open-1",
                    type=QuestionType.OPEN_ENDED,
                    prompt="开放题",
                    reference_answer="参考答案",
                )
            )
            await session.flush()
            prepared = await prepare_evaluation_run(
                session,
                name="Rubric Run",
                agent_version_ids=[version.id],
                dataset_version_ids=[dataset.id],
                judge_config_id=judge.id,
                score_config_id=score_config.id,
            )
            run_id = prepared.run.id

        assert await acquire_worker_lease(engine, owner_id) is True
        assert await claim_next_run(engine, owner_id) == run_id

        async with session_scope(session_factory) as session:
            run = await session.get(EvaluationRun, run_id)
            assert run is not None
            run.status = RunStatus.SCORING
            run.stage = RunStage.JUDGING
            item = await session.scalar(
                select(ExecutionItem).where(ExecutionItem.status == ExecutionStatus.PENDING)
            )
            assert item is not None
            item_id = item.id
            stored = artifact_store.put(
                f"runs/{run_id}/response.json",
                json.dumps({"answer": "候选答案"}).encode(),
            )
            artifact = ArtifactIndex(
                kind=ArtifactKind.AGENT_RESPONSE,
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
            session.add(artifact)
            await session.flush()
            item.status = ExecutionStatus.ANSWERED
            item.answer_preview = "候选答案"
            item.artifact_id = artifact.id

        async def judge_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["model"] == "deepseek-v4-flash"
            assert '"dimensions"' in body["messages"][0]["content"]
            return rubric_response(ALL_DIMENSION_VERDICTS)

        scoring_complete = await execute_answer_scoring(
            session_factory,
            settings,
            run_id,
            artifact_store=artifact_store,
            owner_id=owner_id,
            transport=httpx.MockTransport(judge_handler),
            sleep=no_sleep,
        )
        assert scoring_complete is True

        async with session_factory() as session:
            score = await session.scalar(select(ScoreRecord))
            assert score is not None
            assert float(score.answer_score) == 80.0
            assert score.judge_provider == "deepseek"
            assert score.judge_model_requested == "deepseek-v4-flash"
            assert score.judge_model_returned == "deepseek-v4-flash-20260901"
            assert score.judge_prompt_version == RUBRIC_PROMPT_VERSION
            assert score.judge_temperature == Decimal("0.00")
            assert score.judge_details_json is not None
            dimensions = score.judge_details_json["rubric"]["dimensions"]
            assert len(dimensions) == 4
            weights = {entry["key"]: entry["weight"] for entry in dimensions}
            assert weights == {
                "correctness": 0.4,
                "completeness": 0.3,
                "grounding": 0.2,
                "clarity": 0.1,
            }
            verdicts = {entry["key"]: entry["verdict"] for entry in dimensions}
            assert verdicts == ALL_DIMENSION_VERDICTS

            evidence = await get_run_evidence_detail(
                session, artifact_store, run_id, item_id
            )
            assert evidence.score is not None
            assert evidence.score.judge_prompt_version == RUBRIC_PROMPT_VERSION
            assert evidence.score.judge_model_returned == "deepseek-v4-flash-20260901"
            assert evidence.score.judge_temperature == 0.0
            assert evidence.score.judge_details is not None
            assert len(evidence.score.judge_details["rubric"]["dimensions"]) == 4
    finally:
        await engine.dispose()
