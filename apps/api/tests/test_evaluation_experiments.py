import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from test_runs import (
    RunFixture,
    login_operator,
    migration_config,
    run_client,
    run_payload,
)

from agent_eval.db.enums import (
    AgentVersionLifecycle,
    AuthType,
    RunStatus,
    SessionRole,
)
from agent_eval.db.models import (
    AgentVersion,
    EvaluationExperiment,
    EvaluationRun,
    LeaderboardResult,
    LeaderboardSnapshot,
    RunAgent,
)
from agent_eval.db.session import session_scope
from agent_eval.maintenance.workspace_users import create_workspace_user


def experiment_payload(
    fixture: RunFixture,
    *,
    key: UUID | None = None,
    agent_version_ids: list[UUID] | None = None,
) -> dict[str, object]:
    payload = run_payload(fixture, "Comparison experiment")
    payload["idempotency_key"] = str(key or uuid4())
    payload["agent_version_ids"] = [
        str(value) for value in (agent_version_ids or fixture.agent_version_ids)
    ]
    return payload


def ranking(agent_version_id: UUID, score: float) -> dict[str, object]:
    return {
        "agent_version_id": str(agent_version_id),
        "overall_score": score,
        "ability_scores": {"reasoning": score, "retrieval": score - 5},
        "answer_quality": score,
        "citation_quality": score - 5,
        "success_rate": 100,
        "anomaly_rate": 0,
        "http_call_count": 3,
    }


async def test_experiment_create_list_detail_idempotency_auth_and_results(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        unauthorized = await client.get("/api/v1/evaluation-experiments")
        assert unauthorized.status_code == 401

        mutation_headers = await login_operator(client)
        payload = experiment_payload(fixture)
        csrf_rejected = await client.post(
            "/api/v1/evaluation-experiments", json=payload
        )
        assert csrf_rejected.status_code == 403

        created = await client.post(
            "/api/v1/evaluation-experiments",
            headers=mutation_headers,
            json=payload,
        )
        assert created.status_code == 201
        body = created.json()
        experiment_id = UUID(body["id"])
        assert body["name"] == "Comparison experiment"
        assert body["creator"]["subject_key"] == "legacy_shared:operator"
        assert body["creator"]["user_id"] is not None
        assert body["status"] == "queued"
        assert body["member_count"] == 2
        assert body["progress_percent"] == 0
        assert [member["position"] for member in body["members"]] == [1, 2]
        assert [member["result"] for member in body["members"]] == [None, None]
        assert len({member["run_id"] for member in body["members"]}) == 2
        assert all(member["run_id"] != body["id"] for member in body["members"])

        replay = await client.post(
            "/api/v1/evaluation-experiments",
            headers=mutation_headers,
            json=payload,
        )
        assert replay.status_code == 201
        assert replay.json()["id"] == str(experiment_id)
        conflicting_payload = {**payload, "name": "Changed payload"}
        conflict = await client.post(
            "/api/v1/evaluation-experiments",
            headers=mutation_headers,
            json=conflicting_payload,
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"

        listed = await client.get("/api/v1/evaluation-experiments")
        detail = await client.get(f"/api/v1/evaluation-experiments/{experiment_id}")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [str(experiment_id)]
        assert "members" not in listed.json()[0]
        assert detail.status_code == 200

        first_member = body["members"][0]
        run_state = await client.get(f"/api/v1/evaluation-runs/{first_member['run_id']}")
        run_progress = await client.get(
            f"/api/v1/evaluation-runs/{first_member['run_id']}/progress"
        )
        run_list = await client.get("/api/v1/evaluation-runs")
        assert run_state.json()["experiment_id"] == str(experiment_id)
        assert run_state.json()["experiment_position"] == 1
        assert run_progress.json()["experiment_id"] == str(experiment_id)
        assert {
            item["experiment_position"]
            for item in run_list.json()
            if item["experiment_id"] == str(experiment_id)
        } == {1, 2}

        async with session_scope(app.state.session_factory) as session:
            experiment = await session.get(EvaluationExperiment, experiment_id)
            assert experiment is not None
            assert len(experiment.request_fingerprint) == 64
            assert len(experiment.protocol_hash) == 64
            runs = list(
                await session.scalars(
                    select(EvaluationRun)
                    .options(
                        selectinload(EvaluationRun.run_agents)
                        .joinedload(RunAgent.agent_version)
                    )
                    .where(EvaluationRun.experiment_id == experiment_id)
                    .order_by(EvaluationRun.experiment_position)
                )
            )
            first_run, second_run = runs
            first_agent = first_run.run_agents[0]
            second_agent = second_run.run_agents[0]
            first_agent.completed = first_agent.total
            second_agent.failed = 1
            first_run.status = RunStatus.PUBLISHED
            second_run.status = RunStatus.SYSTEM_FAILED
            first_run.finished_at = datetime.now(UTC)
            second_run.finished_at = datetime.now(UTC)

            first_snapshot = LeaderboardSnapshot(
                run_id=first_run.id,
                rankings_json=[ranking(first_agent.agent_version_id, 80)],
            )
            session.add(first_snapshot)
            await session.flush()
            session.add_all(
                [
                    LeaderboardResult(
                        snapshot_id=first_snapshot.id,
                        run_agent_id=first_agent.id,
                        agent_id=first_agent.agent_version.agent_id,
                        agent_version_id=first_agent.agent_version_id,
                        metric="overall",
                        score=Decimal("80"),
                        protocol_hash="a" * 64,
                        benchmark_count=2,
                    ),
                    LeaderboardResult(
                        snapshot_id=first_snapshot.id,
                        run_agent_id=first_agent.id,
                        agent_id=first_agent.agent_version.agent_id,
                        agent_version_id=first_agent.agent_version_id,
                        metric="reasoning",
                        score=Decimal("80"),
                        protocol_hash="b" * 64,
                        benchmark_count=1,
                    ),
                ]
            )

        partial = await client.get(f"/api/v1/evaluation-experiments/{experiment_id}")
        assert partial.json()["status"] == "system_failed"
        assert partial.json()["processed"] == 4
        assert partial.json()["progress_percent"] == 66.67
        assert partial.json()["members"][0]["result"]["overall_score"] == 80
        assert partial.json()["members"][1]["result"] is None
        assert partial.json()["members"][1]["metric_scores"] == {}
        assert partial.json()["comparison_metrics"] == [
            {"metric": "overall", "protocol_hash": "a" * 64, "compared_count": 1},
            {"metric": "reasoning", "protocol_hash": "b" * 64, "compared_count": 1},
        ]

        async with session_scope(app.state.session_factory) as session:
            second_run = await session.scalar(
                select(EvaluationRun)
                .options(
                    selectinload(EvaluationRun.run_agents)
                    .joinedload(RunAgent.agent_version)
                )
                .where(
                    EvaluationRun.experiment_id == experiment_id,
                    EvaluationRun.experiment_position == 2,
                )
            )
            assert second_run is not None
            second_agent = second_run.run_agents[0]
            second_snapshot = LeaderboardSnapshot(
                run_id=second_run.id,
                rankings_json=[ranking(second_agent.agent_version_id, 70)],
            )
            session.add(second_snapshot)
            await session.flush()
            session.add_all(
                [
                    LeaderboardResult(
                        snapshot_id=second_snapshot.id,
                        run_agent_id=second_agent.id,
                        agent_id=second_agent.agent_version.agent_id,
                        agent_version_id=second_agent.agent_version_id,
                        metric="overall",
                        score=Decimal("70"),
                        protocol_hash="a" * 64,
                        benchmark_count=2,
                    ),
                    LeaderboardResult(
                        snapshot_id=second_snapshot.id,
                        run_agent_id=second_agent.id,
                        agent_id=second_agent.agent_version.agent_id,
                        agent_version_id=second_agent.agent_version_id,
                        metric="reasoning",
                        score=Decimal("70"),
                        protocol_hash="c" * 64,
                        benchmark_count=1,
                    ),
                ]
            )

        compared = await client.get(f"/api/v1/evaluation-experiments/{experiment_id}")
        compared_body = compared.json()
        assert compared_body["comparison_metrics"] == [
            {"metric": "overall", "protocol_hash": "a" * 64, "compared_count": 2}
        ]
        assert [member["metric_ranks"].get("overall") for member in compared_body["members"]] == [
            1,
            2,
        ]
        assert all("reasoning" not in member["metric_ranks"] for member in compared_body["members"])

        async with session_scope(app.state.session_factory) as session:
            standalone = EvaluationRun(
                name="Historical standalone",
                status=RunStatus.CANCELED,
                dataset_bundle_hash="d" * 64,
                protocol_hash="e" * 64,
                judge_config_id=fixture.judge_config_id,
                score_config_id=fixture.score_config_id,
            )
            session.add(standalone)
            await session.flush()
            standalone_id = standalone.id
        standalone_response = await client.get(
            f"/api/v1/evaluation-runs/{standalone_id}"
        )
        assert standalone_response.status_code == 200
        assert standalone_response.json()["experiment_id"] is None
        assert standalone_response.json()["experiment_position"] is None


async def test_experiment_is_atomic_and_rejects_duplicate_logical_agents_and_bad_profile(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        headers = await login_operator(client)
        active = await client.post(
            "/api/v1/evaluation-runs",
            headers=headers,
            json=run_payload(fixture, "Already active"),
        )
        assert active.status_code == 201

        payload = experiment_payload(
            fixture,
            agent_version_ids=[fixture.agent_version_ids[1], fixture.agent_version_ids[0]],
        )
        rejected = await client.post(
            "/api/v1/evaluation-experiments", headers=headers, json=payload
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "evaluation_already_active"
        async with app.state.session_factory() as session:
            assert await session.scalar(
                select(func.count()).select_from(EvaluationExperiment)
            ) == 0
            assert await session.scalar(select(func.count()).select_from(EvaluationRun)) == 1

        async with session_scope(app.state.session_factory) as session:
            original = await session.get(AgentVersion, fixture.agent_version_ids[0])
            assert original is not None
            duplicate = AgentVersion(
                agent_id=original.agent_id,
                version=2,
                endpoint_url="https://agent.example/duplicate-logical",
                auth_type=AuthType.NONE,
                request_template=original.request_template,
                answer_jsonpath=original.answer_jsonpath,
                lifecycle_status=AgentVersionLifecycle.ACTIVE,
            )
            session.add(duplicate)
            await session.flush()
            duplicate_id = duplicate.id

        duplicate_payload = experiment_payload(
            fixture,
            agent_version_ids=[fixture.agent_version_ids[0], duplicate_id],
        )
        duplicate_response = await client.post(
            "/api/v1/evaluation-experiments",
            headers=headers,
            json=duplicate_payload,
        )
        assert duplicate_response.status_code == 422
        assert duplicate_response.json()["detail"]["code"] == "duplicate_logical_agent"

        bad_profile = experiment_payload(
            fixture, agent_version_ids=[fixture.agent_version_ids[1]]
        )
        bad_profile["score_config_id"] = str(uuid4())
        bad_profile_response = await client.post(
            "/api/v1/evaluation-experiments", headers=headers, json=bad_profile
        )
        assert bad_profile_response.status_code == 422
        assert bad_profile_response.json()["detail"]["code"] == "profile_score_config_mismatch"


async def test_experiment_creator_identity_and_database_protocol_constraints(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        async with session_scope(app.state.session_factory) as session:
            named = await create_workspace_user(
                session,
                username="experiment.operator",
                display_name="Experiment Operator",
                role=SessionRole.OPERATOR,
                password="experiment-secret",
            )
            named_id = named.id
        login = await client.post(
            "/api/v1/auth/user-login",
            headers={"Origin": "http://test"},
            json={
                "username": "experiment.operator",
                "password": "experiment-secret",
            },
        )
        assert login.status_code == 200
        csrf = client.cookies.get("agent_eval_csrf")
        assert csrf is not None
        headers = {"Origin": "http://test", "X-CSRF-Token": csrf}
        created = await client.post(
            "/api/v1/evaluation-experiments",
            headers=headers,
            json=experiment_payload(fixture),
        )
        assert created.status_code == 201
        assert created.json()["creator"] == {
            "user_id": str(named_id),
            "subject_key": "local:experiment.operator",
            "display_name": "Experiment Operator",
        }
        experiment_id = UUID(created.json()["id"])

        async with app.state.session_factory() as session:
            runs = list(
                await session.scalars(
                    select(EvaluationRun)
                    .where(EvaluationRun.experiment_id == experiment_id)
                    .order_by(EvaluationRun.experiment_position)
                )
            )
            runs[0].protocol_hash = "f" * 64
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            runs = list(
                await session.scalars(
                    select(EvaluationRun)
                    .where(EvaluationRun.experiment_id == experiment_id)
                    .order_by(EvaluationRun.experiment_position)
                )
            )
            runs[0].experiment_position = None
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            runs = list(
                await session.scalars(
                    select(EvaluationRun)
                    .where(EvaluationRun.experiment_id == experiment_id)
                    .order_by(EvaluationRun.experiment_position)
                )
            )
            runs[1].experiment_position = runs[0].experiment_position
            with pytest.raises(IntegrityError):
                await session.commit()


def test_experiment_migration_preserves_standalone_runs_and_downgrades(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "experiment-migration.db"
    config = migration_config(database_path)
    command.upgrade(config, "f8a1c3d5e702")
    run_id = "10000000000000000000000000000003"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO judge_configs (provider, model, prompt_version, enabled, id, created_at, "
            "updated_at) VALUES ('judge', 'model', 'v1', 1, ?, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)",
            ("10000000000000000000000000000001",),
        )
        connection.execute(
            "INSERT INTO score_configs (name, reasoning_weight, retrieval_weight, data_weight, "
            "instruction_following_weight, long_context_weight, retrieval_answer_weight, "
            "retrieval_citation_weight, citation_authenticity_weight, citation_support_weight, "
            "id, created_at, updated_at) VALUES ('Score', 40, 35, 25, 0, 0, 70, 30, 50, 50, "
            "?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("10000000000000000000000000000002",),
        )
        connection.execute(
            "INSERT INTO evaluation_runs (name, status, desired_state, stage, "
            "dataset_bundle_hash, profile_key, run_scope, ranking_eligible, protocol_hash, "
            "judge_config_id, score_config_id, id, created_at, updated_at) VALUES "
            "('Standalone', 'canceled', 'running', 'preparing', ?, 'comprehensive', "
            "'comprehensive', 1, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (
                "a" * 64,
                "b" * 64,
                "10000000000000000000000000000001",
                "10000000000000000000000000000002",
                run_id,
            ),
        )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(evaluation_runs)")}
        stored = connection.execute(
            "SELECT experiment_id, experiment_position FROM evaluation_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    assert {"experiment_id", "experiment_position"} <= columns
    assert stored == (None, None)

    command.downgrade(config, "f8a1c3d5e702")
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(evaluation_runs)")}
        stored_name = connection.execute(
            "SELECT name FROM evaluation_runs WHERE id = ?", (run_id,)
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert stored_name == ("Standalone",)
    assert "evaluation_experiments" not in tables
    assert not {"experiment_id", "experiment_position"} & columns
