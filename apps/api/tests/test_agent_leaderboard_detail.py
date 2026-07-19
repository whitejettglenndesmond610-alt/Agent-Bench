from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from httpx import AsyncClient
from test_runs import READ_ONLY_PASSWORD, run_client

from agent_eval.db.enums import (
    Ability,
    AgentStatus,
    AgentVersionLifecycle,
    AuthType,
    ResultValidityStatus,
    RunAgentStatus,
    RunScope,
    RunStage,
    RunStatus,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    EvaluationRun,
    LeaderboardResult,
    LeaderboardSnapshot,
    RunAgent,
)
from agent_eval.db.session import session_scope

METRICS = {
    "overall",
    "reasoning",
    "retrieval",
    "data_reasoning",
    "instruction_following",
    "long_context",
}
FORBIDDEN_KEYS = {
    "endpoint_url",
    "auth_type",
    "encrypted_token",
    "token_configured",
    "request_template",
    "answer_jsonpath",
    "citations_jsonpath",
    "preflight_status",
    "preflight_report",
    "prompt",
    "reference_answer",
    "evidence",
    "artifact",
    "rankings",
}


async def login_read_only(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://test"},
        json={"role": "read_only", "password": READ_ONLY_PASSWORD},
    )
    assert response.status_code == 200


def nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for nested in value.values() for key in nested_keys(nested)
        }
    if isinstance(value, list):
        return {key for nested in value for key in nested_keys(nested)}
    return set()


async def test_agent_leaderboard_detail_requires_auth_and_hides_unavailable_agents(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        async with app.state.session_factory() as session:
            alpha_version = await session.get(AgentVersion, fixture.agent_version_ids[0])
            beta_version = await session.get(AgentVersion, fixture.agent_version_ids[1])
            assert alpha_version is not None and beta_version is not None
            alpha_agent_id = alpha_version.agent_id
            beta_agent_id = beta_version.agent_id

        path = f"/api/v1/leaderboards/agents/{alpha_agent_id}"
        assert (await client.get(path)).status_code == 401

        await login_read_only(client)
        response = await client.get(path)
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {
            "agent_id",
            "agent_version_id",
            "agent_name",
            "provider",
            "description",
            "logo_url",
            "version",
            "metrics",
            "score_history",
        }
        assert set(payload["metrics"]) == METRICS
        assert set(payload["score_history"]) == METRICS
        assert all(not points for points in payload["score_history"].values())
        assert not (nested_keys(payload) & FORBIDDEN_KEYS)

        assert (await client.get(path, params={"history_limit": 0})).status_code == 422
        assert (await client.get(path, params={"history_limit": 101})).status_code == 422

        async with session_scope(app.state.session_factory) as session:
            beta = await session.get(Agent, beta_agent_id)
            assert beta is not None
            beta.status = AgentStatus.DISABLED

        disabled = await client.get(f"/api/v1/leaderboards/agents/{beta_agent_id}")
        assert disabled.status_code == 404
        assert disabled.json()["detail"]["code"] == "agent_not_available"
        assert (
            await client.get(f"/api/v1/leaderboards/agents/{uuid4()}")
        ).status_code == 404


async def test_agent_leaderboard_detail_filters_and_orders_official_history(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        await login_read_only(client)
        secret_marker = "must-not-leak-agent-configuration"
        evidence_marker = "must-not-leak-run-evidence"

        async with session_scope(app.state.session_factory) as session:
            alpha_v1 = await session.get(AgentVersion, fixture.agent_version_ids[0])
            beta_v1 = await session.get(AgentVersion, fixture.agent_version_ids[1])
            assert alpha_v1 is not None and beta_v1 is not None
            alpha = await session.get(Agent, alpha_v1.agent_id)
            assert alpha is not None
            alpha.description = "Public Agent description"
            alpha.logo_url = "https://agent.example/logo.svg"
            alpha_v1.lifecycle_status = AgentVersionLifecycle.RETIRED
            alpha_v1.request_template = secret_marker
            alpha_v1.answer_jsonpath = f"$.{secret_marker}"
            alpha_v2 = AgentVersion(
                agent_id=alpha.id,
                version=2,
                endpoint_url=f"https://{secret_marker}.example/run",
                auth_type=AuthType.BEARER,
                encrypted_token=secret_marker,
                request_template=secret_marker,
                answer_jsonpath=f"$.{secret_marker}",
                citations_jsonpath=f"$.{secret_marker}",
                lifecycle_status=AgentVersionLifecycle.ACTIVE,
                enabled=True,
                preflight_report_json=[{"evidence": evidence_marker}],
            )
            session.add(alpha_v2)
            await session.flush()

            async def add_result(
                *,
                name: str,
                published_at: datetime,
                score: str,
                protocol_hash: str,
                version: AgentVersion = alpha_v1,
                metric: str = "reasoning",
                run_status: RunStatus = RunStatus.PUBLISHED,
                ranking_eligible: bool = True,
                validity: ResultValidityStatus = ResultValidityStatus.VALID,
                run_agent_version: AgentVersion | None = None,
                linked_run_agent_id: UUID | None = None,
            ) -> tuple[UUID, UUID, UUID]:
                run = EvaluationRun(
                    name=name,
                    status=run_status,
                    stage=RunStage.READY_TO_PUBLISH,
                    dataset_bundle_hash="9" * 64,
                    profile_key="unified-v3-reasoning",
                    run_scope=RunScope.SPECIALIST,
                    target_ability=Ability.REASONING,
                    ranking_eligible=ranking_eligible,
                    protocol_hash="8" * 64,
                    judge_config_id=fixture.judge_config_id,
                    score_config_id=fixture.score_config_id,
                    system_error=evidence_marker,
                )
                session.add(run)
                await session.flush()
                if linked_run_agent_id is None:
                    linked_version = run_agent_version or version
                    run_agent = RunAgent(
                        run_id=run.id,
                        agent_version_id=linked_version.id,
                        status=RunAgentStatus.COMPLETED,
                        validity_status=validity,
                    )
                    session.add(run_agent)
                    await session.flush()
                    result_run_agent_id = run_agent.id
                else:
                    result_run_agent_id = linked_run_agent_id
                snapshot = LeaderboardSnapshot(
                    run_id=run.id,
                    published_at=published_at,
                    rankings_json=[
                        {
                            "agent_version_id": str(version.id),
                            "request_template": secret_marker,
                            "evidence": evidence_marker,
                            "benchmark_scores": [
                                {
                                    "benchmark_id": str(uuid4()),
                                    "name": "Public benchmark",
                                    "ability": "reasoning",
                                    "language": "zh",
                                    "score": float(score),
                                    "prompt": secret_marker,
                                    "reference_answer": evidence_marker,
                                    "artifact": evidence_marker,
                                }
                            ],
                        }
                    ],
                )
                session.add(snapshot)
                await session.flush()
                session.add(
                    LeaderboardResult(
                        snapshot_id=snapshot.id,
                        run_agent_id=result_run_agent_id,
                        agent_id=version.agent_id,
                        agent_version_id=version.id,
                        metric=metric,
                        score=Decimal(score),
                        protocol_hash=protocol_hash,
                        benchmark_count=1,
                    )
                )
                await session.flush()
                return snapshot.id, run.id, result_run_agent_id

            old_snapshot_id, _, _ = await add_result(
                name="Old protocol v1",
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
                score="70",
                protocol_hash="a" * 64,
            )
            compatible_snapshot_id, _, _ = await add_result(
                name="Current protocol v1",
                published_at=datetime(2026, 1, 2, tzinfo=UTC),
                score="75",
                protocol_hash="b" * 64,
            )
            current_snapshot_id, current_run_id, current_run_agent_id = await add_result(
                name="Current protocol v2",
                published_at=datetime(2026, 1, 3, tzinfo=UTC),
                score="80",
                protocol_hash="b" * 64,
                version=alpha_v2,
            )
            await add_result(
                name="Invalid result",
                published_at=datetime(2026, 1, 4, tzinfo=UTC),
                score="99",
                protocol_hash="c" * 64,
                version=alpha_v2,
                validity=ResultValidityStatus.INVALID,
            )
            await add_result(
                name="Non-ranking result",
                published_at=datetime(2026, 1, 5, tzinfo=UTC),
                score="98",
                protocol_hash="d" * 64,
                version=alpha_v2,
                ranking_eligible=False,
            )
            await add_result(
                name="Unpublished result",
                published_at=datetime(2026, 1, 6, tzinfo=UTC),
                score="97",
                protocol_hash="e" * 64,
                version=alpha_v2,
                run_status=RunStatus.COMPLETED,
            )
            await add_result(
                name="Mismatched normalized result",
                published_at=datetime(2026, 1, 7, tzinfo=UTC),
                score="96",
                protocol_hash="f" * 64,
                version=alpha_v2,
                run_agent_version=beta_v1,
            )
            await add_result(
                name="Cross-run normalized result",
                published_at=datetime(2026, 1, 8, tzinfo=UTC),
                score="95.5",
                protocol_hash="6" * 64,
                version=alpha_v2,
                linked_run_agent_id=current_run_agent_id,
            )
            await add_result(
                name="Unsupported metric",
                published_at=datetime(2026, 1, 9, tzinfo=UTC),
                score="95",
                protocol_hash="7" * 64,
                version=alpha_v2,
                metric="custom_metric",
            )
            agent_id = alpha.id
            alpha_v2_id = alpha_v2.id
            alpha_v1_id = alpha_v1.id

        response = await client.get(
            f"/api/v1/leaderboards/agents/{agent_id}",
            params={"history_limit": 3},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["agent_version_id"] == str(alpha_v2_id)
        assert payload["version"] == 2
        assert payload["description"] == "Public Agent description"
        assert payload["logo_url"] == "https://agent.example/logo.svg"
        assert set(payload["score_history"]) == METRICS
        assert all(
            not payload["score_history"][metric]
            for metric in METRICS - {"reasoning"}
        )

        history = payload["score_history"]["reasoning"]
        assert [point["score"] for point in history] == [70.0, 75.0, 80.0]
        assert [point["agent_version_id"] for point in history] == [
            str(alpha_v1_id),
            str(alpha_v1_id),
            str(alpha_v2_id),
        ]
        assert [point["version"] for point in history] == [1, 1, 2]
        assert [point["protocol_matches_current"] for point in history] == [
            False,
            True,
            True,
        ]
        assert [point["is_current_source"] for point in history] == [False, False, True]
        assert [point["source_snapshot_id"] for point in history] == [
            str(old_snapshot_id),
            str(compatible_snapshot_id),
            str(current_snapshot_id),
        ]
        assert history[-1]["source_run_id"] == str(current_run_id)
        assert history[-1]["source_run_name"] == "Current protocol v2"
        assert history[-1]["source_profile_key"] == "unified-v3-reasoning"
        assert history[-1]["benchmark_count"] == 1
        assert set(history[-1]) == {
            "score",
            "protocol_hash",
            "protocol_matches_current",
            "is_current_source",
            "agent_version_id",
            "version",
            "source_snapshot_id",
            "source_run_id",
            "source_run_name",
            "source_profile_key",
            "source_published_at",
            "benchmark_count",
        }
        assert secret_marker not in response.text
        assert evidence_marker not in response.text
        assert not (nested_keys(payload) & FORBIDDEN_KEYS)

        overview_response = await client.get("/api/v1/leaderboards/global/overview")
        assert overview_response.status_code == 200
        overview_agent = next(
            item
            for item in overview_response.json()["agents"]
            if item["agent_id"] == str(agent_id)
        )
        assert payload["metrics"] == overview_agent["metrics"]

        limited = await client.get(
            f"/api/v1/leaderboards/agents/{agent_id}",
            params={"history_limit": 2},
        )
        assert limited.status_code == 200
        assert [
            point["score"] for point in limited.json()["score_history"]["reasoning"]
        ] == [75.0, 80.0]
