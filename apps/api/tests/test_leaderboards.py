import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from agent_eval.core.settings import Settings
from agent_eval.datasets.balanced_builder import build_balanced_v2_dataset
from agent_eval.datasets.importer import BALANCED_V2_COHORT_KEY, import_question_records
from agent_eval.datasets.schema import load_question_records
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import (
    RELEASED_ABILITIES,
    Ability,
    AgentStatus,
    AuthType,
    DatasetStatus,
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
    ExecutionItem,
    JudgeConfig,
    LeaderboardResult,
    LeaderboardSnapshot,
    Question,
    RunAgent,
    ScoreConfig,
    ScoreRecord,
)
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.leaderboards.publisher import execute_aggregation_and_publish_direct
from agent_eval.runs.profiles import build_run_profiles
from agent_eval.runs.service import prepare_evaluation_run


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


async def test_publisher_writes_metric_results_and_preserves_comprehensive_current(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "leaderboard-publisher.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_scope(session_factory) as session:
            agent = Agent(name="Ranking Agent", provider="Provider", status=AgentStatus.ENABLED)
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
            session.add(version)
            benchmarks = [
                Benchmark(
                    name=f"{ability.value} benchmark",
                    ability=ability,
                    language=Language.ZH,
                    source="https://example.test",
                )
                for ability in Ability
                if ability in RELEASED_ABILITIES
            ]
            session.add_all(benchmarks)
            await session.flush()
            datasets = [
                DatasetVersion(
                    benchmark_id=benchmark.id,
                    version=1,
                    source_version="v1",
                    content_hash=str(index) * 64,
                    question_count=1,
                    status=DatasetStatus.FROZEN,
                    frozen_at=datetime.now(UTC),
                )
                for index, benchmark in enumerate(benchmarks, start=1)
            ]
            judge = JudgeConfig(provider="judge", model="model", prompt_version="v1")
            score_config = ScoreConfig(name="Default")
            session.add_all([*datasets, judge, score_config])
            await session.flush()
            session.add_all(
                [
                    Question(
                        dataset_version_id=dataset.id,
                        external_id=f"q-{index}",
                        type=QuestionType.SHORT_ANSWER,
                        prompt="Question",
                        reference_answer="Answer",
                    )
                    for index, dataset in enumerate(datasets)
                ]
            )
            await session.flush()
            version_id = version.id
            dataset_ids = [dataset.id for dataset in datasets]
            judge_id = judge.id
            score_config_id = score_config.id

        async def publish(
            name: str,
            scope: RunScope,
            selected_dataset_ids: list[UUID],
            *,
            target_ability: Ability | None = None,
            ranking_eligible: bool = True,
        ) -> UUID:
            async with session_scope(session_factory) as session:
                prepared = await prepare_evaluation_run(
                    session,
                    name=name,
                    agent_version_ids=[version_id],
                    dataset_version_ids=selected_dataset_ids,
                    judge_config_id=judge_id,
                    score_config_id=score_config_id,
                    profile_key=name.casefold(),
                    run_scope=scope,
                    target_ability=target_ability,
                    ranking_eligible=ranking_eligible,
                )
                run = prepared.run
                run.status = RunStatus.SCORING
                run.stage = RunStage.AGGREGATING
                run.active_slot = 1
                run.claim_owner_id = "direct-test"
                run.claimed_at = datetime.now(UTC)
                run_agent = await session.scalar(
                    select(RunAgent).where(RunAgent.run_id == run.id)
                )
                assert run_agent is not None
                run_agent.status = RunAgentStatus.COMPLETED
                items = list(
                    await session.scalars(
                        select(ExecutionItem).where(ExecutionItem.run_agent_id == run_agent.id)
                    )
                )
                for index, item in enumerate(items, start=1):
                    item.status = ExecutionStatus.SCORED
                    item.attempt_count = 1
                    session.add(
                        ScoreRecord(
                            execution_item_id=item.id,
                            answer_score=70 + index,
                            final_score=70 + index,
                        )
                    )
                run_id = run.id
            assert await execute_aggregation_and_publish_direct(session_factory, run_id) is True
            return run_id

        comprehensive_id = await publish(
            "Comprehensive", RunScope.COMPREHENSIVE, dataset_ids
        )
        specialist_id = await publish(
            "Reasoning",
            RunScope.SPECIALIST,
            [dataset_ids[0]],
            target_ability=Ability.REASONING,
        )
        assert await execute_aggregation_and_publish_direct(session_factory, specialist_id) is True
        smoke_id = await publish(
            "Smoke",
            RunScope.SMOKE,
            [dataset_ids[0]],
            ranking_eligible=False,
        )

        async with session_factory() as session:
            snapshots = list(await session.scalars(select(LeaderboardSnapshot)))
            results = list(await session.scalars(select(LeaderboardResult)))
        snapshots_by_run = {snapshot.run_id: snapshot for snapshot in snapshots}
        comprehensive_results = [
            result
            for result in results
            if result.snapshot_id == snapshots_by_run[comprehensive_id].id
        ]
        specialist_results = [
            result
            for result in results
            if result.snapshot_id == snapshots_by_run[specialist_id].id
        ]
        smoke_results = [
            result for result in results if result.snapshot_id == snapshots_by_run[smoke_id].id
        ]

        assert {result.metric for result in comprehensive_results} == {
            "overall",
            "reasoning",
            "retrieval",
            "data_reasoning",
        }
        assert {result.metric for result in specialist_results} == {"reasoning"}
        assert smoke_results == []
        assert snapshots_by_run[comprehensive_id].is_current is False
        assert snapshots_by_run[specialist_id].is_current is False
        assert snapshots_by_run[smoke_id].is_current is False
        comprehensive_reasoning = next(
            result for result in comprehensive_results if result.metric == "reasoning"
        )
        assert comprehensive_reasoning.protocol_hash == specialist_results[0].protocol_hash
        assert comprehensive_reasoning.benchmark_count == 1
    finally:
        await engine.dispose()


async def test_unified_v3_publisher_emits_all_metrics_and_becomes_current(
    tmp_path: Path,
) -> None:
    output = tmp_path / "balanced-v2"
    build_balanced_v2_dataset(Path("data/demo-dataset"), output)
    database_path = tmp_path / "balanced-publisher.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    engine = create_engine(
        Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    )
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            await import_question_records(
                session, load_question_records(Path("data/demo-dataset"))
            )
            await import_question_records(
                session,
                load_question_records(output),
                cohort_key=BALANCED_V2_COHORT_KEY,
                allow_candidate=True,
            )
            agent = Agent(name="Balanced Agent", provider="Provider")
            invalid_agent = Agent(name="Misconfigured Agent", provider="Provider")
            judge = JudgeConfig(provider="judge", model="model", prompt_version="v1")
            session.add_all([agent, invalid_agent, judge])
            await session.flush()
            version = AgentVersion(
                agent_id=agent.id,
                version=1,
                endpoint_url="https://agent.example/run",
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
                citations_jsonpath="$.citations",
            )
            session.add(version)
            invalid_version = AgentVersion(
                agent_id=invalid_agent.id,
                version=1,
                endpoint_url="https://invalid-agent.example/run",
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
                citations_jsonpath="$.citations",
            )
            session.add(invalid_version)
            await session.flush()
            profile = next(
                item for item in await build_run_profiles(session) if item.key == "unified-v3"
            )
            assert profile.score_config_id is not None
            prepared = await prepare_evaluation_run(
                session,
                name="Unified V3",
                agent_version_ids=[version.id, invalid_version.id],
                dataset_version_ids=list(profile.dataset_version_ids),
                judge_config_id=judge.id,
                score_config_id=profile.score_config_id,
                profile_key=profile.key,
            )
            prepared.run.status = RunStatus.SCORING
            prepared.run.stage = RunStage.AGGREGATING
            prepared.run.active_slot = 1
            prepared.run.claim_owner_id = "direct-test"
            prepared.run.claimed_at = datetime.now(UTC)
            run_agents = list(
                await session.scalars(
                    select(RunAgent)
                    .where(RunAgent.run_id == prepared.run.id)
                    .order_by(RunAgent.agent_version_id)
                )
            )
            assert len(run_agents) == 2
            questions = {
                question.id: question
                for question in await session.scalars(select(Question))
            }
            for run_agent in run_agents:
                run_agent.status = RunAgentStatus.COMPLETED
                items = list(
                    await session.scalars(
                        select(ExecutionItem)
                        .where(ExecutionItem.run_agent_id == run_agent.id)
                        .order_by(ExecutionItem.id)
                    )
                )
                is_invalid = run_agent.agent_version_id == invalid_version.id
                for index, item in enumerate(items):
                    configuration_failure = is_invalid and index < len(items) // 5
                    item.status = (
                        ExecutionStatus.FAILED
                        if configuration_failure
                        else ExecutionStatus.SCORED
                    )
                    item.error_type = "agent_client_error" if configuration_failure else None
                    question = questions[item.question_id]
                    session.add(
                        ScoreRecord(
                            execution_item_id=item.id,
                            answer_score=0 if configuration_failure else 80,
                            citation_score=(
                                80
                                if question.requires_citations and not configuration_failure
                                else None
                            ),
                            final_score=0 if configuration_failure else 80,
                        )
                    )
            run_id = prepared.run.id
            valid_run_agent_id = next(
                item.id for item in run_agents if item.agent_version_id == version.id
            )
            invalid_run_agent_id = next(
                item.id for item in run_agents if item.agent_version_id == invalid_version.id
            )

        assert await execute_aggregation_and_publish_direct(session_factory, run_id)
        async with session_factory() as session:
            results = list(
                await session.scalars(
                    select(LeaderboardResult).join(LeaderboardSnapshot).where(
                        LeaderboardSnapshot.run_id == run_id
                    )
                )
            )
            snapshot = await session.scalar(
                select(LeaderboardSnapshot).where(LeaderboardSnapshot.run_id == run_id)
            )
            persisted_agents = {
                item.id: item
                for item in await session.scalars(
                    select(RunAgent).where(RunAgent.run_id == run_id)
                )
            }
        assert {result.metric for result in results} == {
            "overall",
            *(ability.value for ability in Ability if ability is not Ability.AGENTIC),
        }
        assert {float(result.score) for result in results} == {80.0}
        assert snapshot is not None and snapshot.is_current is True
        assert {result.run_agent_id for result in results} == {valid_run_agent_id}
        assert persisted_agents[valid_run_agent_id].validity_status == ResultValidityStatus.VALID
        invalid_result = persisted_agents[invalid_run_agent_id]
        assert invalid_result.validity_status == ResultValidityStatus.INVALID
        assert invalid_result.configuration_failure_count == 35
        assert float(invalid_result.configuration_failure_rate) == 20
        assert len(snapshot.rankings_json) == 2
        ranking_validity = {
            item["run_agent_id"]: item["validity_status"] for item in snapshot.rankings_json
        }
        assert ranking_validity == {
            str(valid_run_agent_id): "valid",
            str(invalid_run_agent_id): "invalid",
        }

        async with session_scope(session_factory) as session:
            rejected = await prepare_evaluation_run(
                session,
                name="Unified V3 all invalid",
                agent_version_ids=[version.id, invalid_version.id],
                dataset_version_ids=list(profile.dataset_version_ids),
                judge_config_id=judge.id,
                score_config_id=profile.score_config_id,
                profile_key=profile.key,
            )
            rejected.run.status = RunStatus.SCORING
            rejected.run.stage = RunStage.AGGREGATING
            rejected.run.active_slot = 1
            rejected.run.claim_owner_id = "direct-test"
            rejected.run.claimed_at = datetime.now(UTC)
            rejected_agents = list(
                await session.scalars(
                    select(RunAgent).where(RunAgent.run_id == rejected.run.id)
                )
            )
            rejected_questions = {
                question.id: question for question in await session.scalars(select(Question))
            }
            for run_agent in rejected_agents:
                run_agent.status = RunAgentStatus.COMPLETED
                items = list(
                    await session.scalars(
                        select(ExecutionItem)
                        .where(ExecutionItem.run_agent_id == run_agent.id)
                        .order_by(ExecutionItem.id)
                    )
                )
                for index, item in enumerate(items):
                    authentication_failure = index == 0
                    item.status = (
                        ExecutionStatus.FAILED
                        if authentication_failure
                        else ExecutionStatus.SCORED
                    )
                    item.error_type = (
                        "agent_authentication" if authentication_failure else None
                    )
                    question = rejected_questions[item.question_id]
                    session.add(
                        ScoreRecord(
                            execution_item_id=item.id,
                            answer_score=0 if authentication_failure else 80,
                            citation_score=(
                                80
                                if question.requires_citations and not authentication_failure
                                else None
                            ),
                            final_score=0 if authentication_failure else 80,
                        )
                    )
            rejected_run_id = rejected.run.id

        assert await execute_aggregation_and_publish_direct(session_factory, rejected_run_id)
        async with session_factory() as session:
            rejected_snapshot = await session.scalar(
                select(LeaderboardSnapshot).where(
                    LeaderboardSnapshot.run_id == rejected_run_id
                )
            )
            rejected_results = list(
                await session.scalars(
                    select(LeaderboardResult)
                    .join(LeaderboardSnapshot)
                    .where(LeaderboardSnapshot.run_id == rejected_run_id)
                )
            )
            original_snapshot = await session.scalar(
                select(LeaderboardSnapshot).where(LeaderboardSnapshot.run_id == run_id)
            )
        assert rejected_snapshot is not None and rejected_snapshot.is_current is False
        assert rejected_results == []
        assert original_snapshot is not None and original_snapshot.is_current is True
    finally:
        await engine.dispose()
