import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from agent_eval.api.main import create_app
from agent_eval.core.artifact_store import StoredArtifact, create_artifact_store
from agent_eval.core.settings import Settings
from agent_eval.db.base import utc_now
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import (
    AgentVersionLifecycle,
    AuthType,
    DatasetReleaseStatus,
    EvaluationCaseStatus,
    SessionRole,
    WorkspaceAttemptStatus,
    WorkspaceMessageRole,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    ArtifactIndex,
    DatasetRelease,
    DatasetVersion,
    EvaluationCase,
    EvaluationRun,
    JudgeConfig,
    LeaderboardResult,
    LeaderboardSnapshot,
    Question,
    RunDatasetVersion,
    ScoreConfig,
    WorkspaceAttempt,
    WorkspaceComparison,
    WorkspaceConversation,
    WorkspaceMessage,
)
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.maintenance.workspace_users import create_workspace_user
from agent_eval.runs.profiles import build_run_profiles
from agent_eval.runs.queue import acquire_worker_lease
from agent_eval.worker.limits import WorkerLimits
from agent_eval.workspace.executor import process_workspace_attempt
from agent_eval.workspace.queue import (
    WorkspaceClaim,
    claim_next_workspace_attempt,
    fail_stale_workspace_attempts,
)

OPERATOR_PASSWORD = "operator-secret"
password_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


@asynccontextmanager
async def workspace_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, FastAPI, Settings, UUID, UUID]]:
    database_path = tmp_path / "workspace.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        data_dir=tmp_path,
        environment="test",
        read_only_password_hash=password_hasher.hash("read-only-secret"),
        operator_password_hash=password_hasher.hash(OPERATOR_PASSWORD),
        worker_poll_seconds=0.05,
    )
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    try:
        async with session_scope(sessions) as session:
            agent = Agent(
                name="Workspace Agent",
                provider="Workspace Provider",
                description="Safe catalog description",
            )
            small_agent = Agent(name="Small Context Agent", provider="Workspace Provider")
            session.add_all([agent, small_agent])
            await session.flush()
            version = AgentVersion(
                agent_id=agent.id,
                version=1,
                endpoint_url="https://agent.example/workspace",
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
                citations_jsonpath="$.citations",
                lifecycle_status=AgentVersionLifecycle.ACTIVE,
                concurrency=2,
                max_prompt_characters=10_000,
            )
            small_version = AgentVersion(
                agent_id=small_agent.id,
                version=1,
                endpoint_url="https://agent.example/small",
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
                lifecycle_status=AgentVersionLifecycle.ACTIVE,
                max_prompt_characters=20,
            )
            session.add_all([version, small_version])
            await session.flush()
            version_id = version.id
            small_version_id = small_version.id
    finally:
        await engine.dispose()

    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client, app, settings, version_id, small_version_id


async def login_operator(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://test"},
        json={"role": "operator", "password": OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    csrf = client.cookies.get("agent_eval_csrf")
    assert csrf is not None
    return {"Origin": "http://test", "X-CSRF-Token": csrf}


async def run_next_attempt(
    app: FastAPI,
    settings: Settings,
    handler: httpx.MockTransport,
    *,
    owner_id: str = "workspace-worker",
) -> UUID:
    assert await acquire_worker_lease(
        app.state.engine, owner_id, lease_seconds=settings.worker_lease_seconds
    )
    claim = await claim_next_workspace_attempt(
        app.state.engine,
        owner_id,
        lease_seconds=settings.worker_lease_seconds,
    )
    assert claim is not None
    completed = await process_workspace_attempt(
        app.state.session_factory,
        settings,
        claim,
        artifact_store=app.state.artifact_store,
        token_cipher=None,
        owner_id=owner_id,
        limits=WorkerLimits.from_settings(settings),
        transport=handler,
    )
    assert completed
    return claim.attempt_id


async def test_concurrent_sqlite_send_is_idempotent_and_conflict_safe(
    tmp_path: Path,
) -> None:
    async with workspace_client(tmp_path) as (client, _, _, version_id, _):
        headers = await login_operator(client)

        async def create(title: str) -> str:
            response = await client.post(
                "/api/v1/workspace/conversations",
                headers=headers,
                json={"agent_version_id": str(version_id), "title": title},
            )
            assert response.status_code == 201
            return response.json()["id"]

        conversation_id = await create("Same key")
        key = str(uuid4())
        payload = {
            "content": "Concurrent message",
            "idempotency_key": key,
            "expected_revision": 0,
        }
        same_key = await asyncio.gather(
            client.post(
                f"/api/v1/workspace/conversations/{conversation_id}/messages",
                headers=headers,
                json=payload,
            ),
            client.post(
                f"/api/v1/workspace/conversations/{conversation_id}/messages",
                headers=headers,
                json=payload,
            ),
        )
        assert [response.status_code for response in same_key] == [202, 202]
        assert len({response.json()["attempt"]["id"] for response in same_key}) == 1

        revision_conversation_id = await create("Different keys")
        different_keys = await asyncio.gather(
            client.post(
                f"/api/v1/workspace/conversations/{revision_conversation_id}/messages",
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json={"content": "First contender", "expected_revision": 0},
            ),
            client.post(
                f"/api/v1/workspace/conversations/{revision_conversation_id}/messages",
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json={"content": "Second contender", "expected_revision": 0},
            ),
        )
        assert sorted(response.status_code for response in different_keys) == [202, 409]
        conflict = next(response for response in different_keys if response.status_code == 409)
        assert conflict.json()["detail"]["code"] == "revision_conflict"

        fingerprint_conversation_id = await create("Different fingerprint")
        fingerprint_key = str(uuid4())
        different_fingerprints = await asyncio.gather(
            client.post(
                f"/api/v1/workspace/conversations/{fingerprint_conversation_id}/messages",
                headers=headers,
                json={
                    "content": "Fingerprint A",
                    "idempotency_key": fingerprint_key,
                    "expected_revision": 0,
                },
            ),
            client.post(
                f"/api/v1/workspace/conversations/{fingerprint_conversation_id}/messages",
                headers=headers,
                json={
                    "content": "Fingerprint B",
                    "idempotency_key": fingerprint_key,
                    "expected_revision": 0,
                },
            ),
        )
        assert sorted(response.status_code for response in different_fingerprints) == [202, 409]
        conflict = next(
            response for response in different_fingerprints if response.status_code == 409
        )
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"


async def test_workspace_comparison_and_curated_evaluation_case(tmp_path: Path) -> None:
    provider_prompts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        provider_prompts.append(json.loads(request.content)["query"])
        return httpx.Response(
            200,
            json={
                "answer": "候选答案",
                "citations": ["https://example.test/evidence"],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                    "total_tokens": 8,
                },
            },
        )

    async with workspace_client(tmp_path) as (
        client,
        app,
        settings,
        version_id,
        small_version_id,
    ):
        async with session_scope(app.state.session_factory) as session:
            named = await create_workspace_user(
                session,
                username="comparison.operator",
                display_name="Comparison Operator",
                role=SessionRole.OPERATOR,
                password="comparison-secret",
            )
            named_id = named.id
        login = await client.post(
            "/api/v1/auth/user-login",
            headers={"Origin": "http://test"},
            json={"username": "comparison.operator", "password": "comparison-secret"},
        )
        assert login.status_code == 200
        csrf = client.cookies.get("agent_eval_csrf")
        assert csrf is not None
        headers = {"Origin": "http://test", "X-CSRF-Token": csrf}

        oversized = await client.post(
            "/api/v1/workspace/comparisons",
            headers=headers,
            json={
                "prompt": "x" * 21,
                "agent_version_ids": [str(version_id), str(small_version_id)],
                "idempotency_key": str(uuid4()),
            },
        )
        assert oversized.status_code == 422
        assert oversized.json()["detail"]["code"] == "prompt_too_long"
        async with app.state.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(WorkspaceComparison)) == 0

        key = str(uuid4())
        payload = {
            "prompt": "共同问题",
            "agent_version_ids": [str(version_id), str(small_version_id)],
            "idempotency_key": key,
        }
        created = await client.post(
            "/api/v1/workspace/comparisons", headers=headers, json=payload
        )
        assert created.status_code == 202
        comparison_id = created.json()["id"]
        assert created.json()["status"] == "queued"
        assert len(created.json()["items"]) == 2

        replay = await client.post(
            "/api/v1/workspace/comparisons", headers=headers, json=payload
        )
        assert replay.status_code == 202
        assert replay.json()["id"] == comparison_id
        conflict = await client.post(
            "/api/v1/workspace/comparisons",
            headers=headers,
            json={**payload, "prompt": "不同问题"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"
        assert (await client.get("/api/v1/workspace/conversations")).json() == {
            "items": []
        }
        hidden_id = created.json()["items"][0]["conversation_id"]
        assert (
            await client.get(f"/api/v1/workspace/conversations/{hidden_id}")
        ).status_code == 404

        async with app.state.session_factory() as session:
            attempts = list(
                await session.scalars(
                    select(WorkspaceAttempt)
                    .join(
                        WorkspaceConversation,
                        WorkspaceConversation.id == WorkspaceAttempt.conversation_id,
                    )
                    .where(WorkspaceConversation.comparison_id == UUID(comparison_id))
                )
            )
        assert len({attempt.prompt_hash for attempt in attempts}) == 1
        assert {attempt.prompt_format for attempt in attempts} == {"workspace-compare-v1"}

        await run_next_attempt(app, settings, httpx.MockTransport(handler))
        await run_next_attempt(app, settings, httpx.MockTransport(handler))
        detail = await client.get(f"/api/v1/workspace/comparisons/{comparison_id}")
        assert detail.json()["status"] == "completed"
        assert detail.json()["counts"]["succeeded"] == 2
        assert provider_prompts == ["共同问题", "共同问题"]

        source_attempt_id = detail.json()["items"][0]["attempt"]["id"]
        draft_key = str(uuid4())
        draft = await client.post(
            "/api/v1/evaluation-cases",
            headers=headers,
            json={
                "source_attempt_id": source_attempt_id,
                "idempotency_key": draft_key,
            },
        )
        assert draft.status_code == 201
        case_id = draft.json()["id"]
        assert draft.json()["status"] == "draft"
        assert draft.json()["reference_answer"] is None
        assert draft.json()["source"]["candidate_answer"] == "候选答案"
        draft_replay = await client.post(
            "/api/v1/evaluation-cases",
            headers=headers,
            json={
                "source_attempt_id": source_attempt_id,
                "idempotency_key": draft_key,
            },
        )
        assert draft_replay.status_code == 201
        assert draft_replay.json()["id"] == case_id
        duplicate = await client.post(
            "/api/v1/evaluation-cases",
            headers=headers,
            json={
                "source_attempt_id": source_attempt_id,
                "idempotency_key": str(uuid4()),
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "source_already_curated"
        artifact_sha = draft.json()["source"]["artifact_sha256"]
        assert artifact_sha is not None
        async with session_scope(app.state.session_factory) as session:
            stored_attempt = await session.get(WorkspaceAttempt, UUID(source_attempt_id))
            assert stored_attempt is not None and stored_attempt.artifact_id is not None
            stored_artifact = await session.get(ArtifactIndex, stored_attempt.artifact_id)
            assert stored_artifact is not None
            await session.delete(stored_artifact)
        persisted_source = await client.get(f"/api/v1/evaluation-cases/{case_id}")
        assert persisted_source.json()["source"]["artifact_sha256"] == artifact_sha

        edited = await client.patch(
            f"/api/v1/evaluation-cases/{case_id}",
            headers=headers,
            json={
                "expected_revision": 0,
                "reference_answer": "人工核验后的标准答案",
                "language": "zh",
                "ability": "reasoning",
                "question_type": "short_answer",
                "difficulty": "medium",
                "domain": "general",
            },
        )
        assert edited.status_code == 200
        reviewed = await client.post(
            f"/api/v1/evaluation-cases/{case_id}/review",
            headers=headers,
            json={
                "expected_revision": 1,
                "standalone_verified": True,
                "reference_answer_verified": True,
                "rights_verified": True,
                "sensitive_data_verified": True,
            },
        )
        assert reviewed.json()["status"] == "reviewed"
        assert reviewed.json()["record_hash"] is not None
        reset_to_draft = await client.patch(
            f"/api/v1/evaluation-cases/{case_id}",
            headers=headers,
            json={"expected_revision": 2, "review_notes": "补充复核备注"},
        )
        assert reset_to_draft.json()["status"] == "draft"
        assert reset_to_draft.json()["record_hash"] is None
        reviewed_again = await client.post(
            f"/api/v1/evaluation-cases/{case_id}/review",
            headers=headers,
            json={
                "expected_revision": 3,
                "standalone_verified": True,
                "reference_answer_verified": True,
                "rights_verified": True,
                "sensitive_data_verified": True,
            },
        )
        assert reviewed_again.json()["status"] == "reviewed"
        published = await client.post(
            f"/api/v1/evaluation-cases/{case_id}/publish",
            headers=headers,
            json={"expected_revision": 4, "confirm_curated_only": True},
        )
        assert published.json()["status"] == "published"
        async with app.state.session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    update(EvaluationCase)
                    .where(EvaluationCase.id == UUID(case_id))
                    .values(rights_verified=False)
                )
                await session.commit()
            await session.rollback()
        immutable = await client.patch(
            f"/api/v1/evaluation-cases/{case_id}",
            headers=headers,
            json={"expected_revision": 5, "prompt": "不能修改"},
        )
        assert immutable.status_code == 409

        second_source_attempt_id = detail.json()["items"][1]["attempt"]["id"]
        concurrent_case_creates = await asyncio.gather(
            client.post(
                "/api/v1/evaluation-cases",
                headers=headers,
                json={
                    "source_attempt_id": second_source_attempt_id,
                    "idempotency_key": str(uuid4()),
                },
            ),
            client.post(
                "/api/v1/evaluation-cases",
                headers=headers,
                json={
                    "source_attempt_id": second_source_attempt_id,
                    "idempotency_key": str(uuid4()),
                },
            ),
        )
        assert sorted(response.status_code for response in concurrent_case_creates) == [201, 409]
        second_case = next(
            response for response in concurrent_case_creates if response.status_code == 201
        )
        duplicate_case = next(
            response for response in concurrent_case_creates if response.status_code == 409
        )
        assert duplicate_case.json()["detail"]["code"] == "source_already_curated"
        concurrent_case_updates = await asyncio.gather(
            client.patch(
                f"/api/v1/evaluation-cases/{second_case.json()['id']}",
                headers=headers,
                json={"expected_revision": 0, "review_notes": "first"},
            ),
            client.patch(
                f"/api/v1/evaluation-cases/{second_case.json()['id']}",
                headers=headers,
                json={"expected_revision": 0, "review_notes": "second"},
            ),
        )
        assert sorted(response.status_code for response in concurrent_case_updates) == [200, 409]
        assert next(
            response for response in concurrent_case_updates if response.status_code == 409
        ).json()["detail"]["code"] == "revision_conflict"

        cancel_payload = {
            "prompt": "取消对比",
            "agent_version_ids": [str(version_id), str(small_version_id)],
            "idempotency_key": str(uuid4()),
        }
        cancel_target = await client.post(
            "/api/v1/workspace/comparisons", headers=headers, json=cancel_payload
        )
        canceled = await client.post(
            f"/api/v1/workspace/comparisons/{cancel_target.json()['id']}/cancel",
            headers=headers,
        )
        assert canceled.json()["counts"]["canceled"] == 2
        assert canceled.json()["status"] == "completed"

        concurrent_key = str(uuid4())
        concurrent_payload = {
            "prompt": "并发幂等对比",
            "agent_version_ids": [str(version_id), str(small_version_id)],
            "idempotency_key": concurrent_key,
        }
        concurrent_comparisons = await asyncio.gather(
            client.post(
                "/api/v1/workspace/comparisons", headers=headers, json=concurrent_payload
            ),
            client.post(
                "/api/v1/workspace/comparisons", headers=headers, json=concurrent_payload
            ),
        )
        assert [response.status_code for response in concurrent_comparisons] == [202, 202]
        assert len({response.json()["id"] for response in concurrent_comparisons}) == 1
        await client.post(
            f"/api/v1/workspace/comparisons/{concurrent_comparisons[0].json()['id']}/cancel",
            headers=headers,
        )

        async with app.state.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(WorkspaceComparison)) == 3
            assert await session.scalar(select(func.count()).select_from(EvaluationCase)) == 2
            stored_case = await session.get(EvaluationCase, UUID(case_id))
            assert stored_case is not None
            assert stored_case.owner_user_id == named_id
            assert stored_case.status == EvaluationCaseStatus.PUBLISHED
            for model in (
                DatasetVersion,
                Question,
                EvaluationRun,
                LeaderboardSnapshot,
                LeaderboardResult,
            ):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert app.state.sqlite_mutation_locks == {}

        published_cases = await client.get(
            "/api/v1/evaluation-cases?status=published"
        )
        assert [item["id"] for item in published_cases.json()["items"]] == [case_id]
        blank_release = await client.post(
            "/api/v1/dataset-releases",
            headers=headers,
            json={
                "name": "   ",
                "evaluation_case_ids": [case_id],
                "idempotency_key": str(uuid4()),
            },
        )
        assert blank_release.status_code == 422

        release_key = str(uuid4())
        release = await client.post(
            "/api/v1/dataset-releases",
            headers=headers,
            json={
                "name": "Workspace curated release",
                "description": "Reviewed production candidates",
                "evaluation_case_ids": [case_id],
                "idempotency_key": release_key,
            },
        )
        assert release.status_code == 201
        release_id = release.json()["id"]
        assert release.json()["status"] == "draft"
        assert release.json()["dataset_versions"] == []
        replayed_release = await client.post(
            "/api/v1/dataset-releases",
            headers=headers,
            json={
                "name": "Workspace curated release",
                "description": "Reviewed production candidates",
                "evaluation_case_ids": [case_id],
                "idempotency_key": release_key,
            },
        )
        assert replayed_release.json()["id"] == release_id

        approved_release = await client.post(
            f"/api/v1/dataset-releases/{release_id}/approve",
            headers=headers,
            json={"expected_revision": 0, "confirm_case_selection": True},
        )
        assert approved_release.json()["status"] == "approved"
        assert approved_release.json()["manifest_hash"] is not None
        assert approved_release.json()["dataset_versions"] == []

        published_release = await client.post(
            f"/api/v1/dataset-releases/{release_id}/publish",
            headers=headers,
            json={"expected_revision": 1, "confirm_not_run_eligible": True},
        )
        assert published_release.json()["status"] == "published"
        assert len(published_release.json()["dataset_versions"]) == 1
        assert published_release.json()["dataset_versions"][0]["run_eligible"] is False
        released_version_id = UUID(
            published_release.json()["dataset_versions"][0]["id"]
        )

        async with app.state.session_factory() as session:
            stored_release = await session.get(DatasetRelease, UUID(release_id))
            released_question = await session.scalar(
                select(Question).where(
                    Question.source_evaluation_case_id == UUID(case_id)
                )
            )
            profiles = await build_run_profiles(session)
            assert stored_release is not None
            assert stored_release.status == DatasetReleaseStatus.PUBLISHED
            assert released_question is not None
            assert all(
                released_version_id not in profile.dataset_version_ids
                for profile in profiles
            )
            assert await session.scalar(
                select(func.count()).select_from(LeaderboardResult)
            ) == 0

            judge = JudgeConfig(provider="test", model="test", prompt_version="v1")
            score = ScoreConfig(name="Curated release isolation")
            session.add_all([judge, score])
            await session.flush()
            isolated_run = EvaluationRun(
                name="Must reject curated release",
                dataset_bundle_hash="c" * 64,
                protocol_hash="d" * 64,
                judge_config_id=judge.id,
                score_config_id=score.id,
            )
            session.add(isolated_run)
            await session.flush()
            session.add(
                RunDatasetVersion(
                    run_id=isolated_run.id,
                    dataset_version_id=released_version_id,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        await login_operator(client)
        shared_draft = await client.post(
            "/api/v1/evaluation-cases",
            headers={
                "Origin": "http://test",
                "X-CSRF-Token": client.cookies.get("agent_eval_csrf") or "",
            },
            json={
                "source_attempt_id": source_attempt_id,
                "idempotency_key": str(uuid4()),
            },
        )
        assert shared_draft.status_code == 403
        assert shared_draft.json()["detail"]["code"] == "named_operator_required"
        assert (
            await client.get(f"/api/v1/workspace/comparisons/{comparison_id}")
        ).status_code == 404
        assert (await client.get("/api/v1/evaluation-cases")).json() == {"items": []}
        assert (await client.get("/api/v1/dataset-releases")).json() == {"items": []}


async def test_workspace_api_context_regeneration_and_ownership(tmp_path: Path) -> None:
    prompts: list[str] = []
    answers = iter(
        ["Answer v1", None, "Answer v2", "Second turn answer", "Repinned answer"]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["query"]
        prompts.append(prompt)
        answer = next(answers)
        if answer is None:
            return httpx.Response(400, json={"error": "invalid request"})
        return httpx.Response(
            200,
            json={
                "answer": answer,
                "citations": ["https://example.test/source"],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            },
        )

    async with workspace_client(tmp_path) as (
        client,
        app,
        settings,
        version_id,
        small_version_id,
    ):
        read_only = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://test"},
            json={"role": "read_only", "password": "read-only-secret"},
        )
        assert read_only.status_code == 200
        assert read_only.json()["workspace_enabled"] is False
        assert (await client.get("/api/v1/workspace/agents")).status_code == 403

        headers = await login_operator(client)
        catalog = await client.get("/api/v1/workspace/agents")
        assert catalog.status_code == 200
        assert len(catalog.json()) == 2
        serialized_catalog = catalog.text
        assert "endpoint_url" not in serialized_catalog
        assert "request_template" not in serialized_catalog
        assert "encrypted_token" not in serialized_catalog

        missing_csrf = await client.post(
            "/api/v1/workspace/conversations",
            headers={"Origin": "http://test"},
            json={"agent_version_id": str(version_id), "title": "Missing CSRF"},
        )
        assert missing_csrf.status_code == 403

        created = await client.post(
            "/api/v1/workspace/conversations",
            headers=headers,
            json={"agent_version_id": str(version_id), "title": "Durable chat"},
        )
        assert created.status_code == 201
        conversation_id = UUID(created.json()["id"])
        assert created.json()["revision"] == 0

        first_key = uuid4()
        first_payload = {
            "content": "First question",
            "idempotency_key": str(first_key),
            "expected_revision": 0,
        }
        first = await client.post(
            f"/api/v1/workspace/conversations/{conversation_id}/messages",
            headers=headers,
            json=first_payload,
        )
        assert first.status_code == 202
        first_attempt_id = first.json()["attempt"]["id"]
        first_user_message_id = first.json()["user_message"]["id"]

        replay = await client.post(
            f"/api/v1/workspace/conversations/{conversation_id}/messages",
            headers=headers,
            json=first_payload,
        )
        assert replay.status_code == 202
        assert replay.json()["attempt"]["id"] == first_attempt_id

        conflicting_replay = await client.post(
            f"/api/v1/workspace/conversations/{conversation_id}/messages",
            headers=headers,
            json={**first_payload, "content": "Different"},
        )
        assert conflicting_replay.status_code == 409
        assert conflicting_replay.json()["detail"]["code"] == "idempotency_conflict"

        canceled = await client.post(
            f"/api/v1/workspace/attempts/{first_attempt_id}/cancel", headers=headers
        )
        assert canceled.status_code == 200
        assert canceled.json()["status"] == "canceled"
        canceled_again = await client.post(
            f"/api/v1/workspace/attempts/{first_attempt_id}/cancel", headers=headers
        )
        assert canceled_again.json()["status"] == "canceled"

        regenerate = await client.post(
            f"/api/v1/workspace/conversations/{conversation_id}/messages/"
            f"{first_user_message_id}/regenerate",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"expected_revision": 1},
        )
        assert regenerate.status_code == 202
        assert regenerate.json()["attempt"]["generation_no"] == 2
        await run_next_attempt(app, settings, httpx.MockTransport(handler))

        succeeded = await client.get(
            f"/api/v1/workspace/attempts/{regenerate.json()['attempt']['id']}"
        )
        assert succeeded.json()["status"] == "succeeded"
        assert succeeded.json()["assistant_message"]["content"] == "Answer v1"
        assert succeeded.json()["total_tokens"] == 6
        assert succeeded.json()["http_attempt_count"] == 1
        assert succeeded.json()["latency_ms"] >= 0

        failed_regen = await client.post(
            f"/api/v1/workspace/conversations/{conversation_id}/messages/"
            f"{first_user_message_id}/regenerate",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"expected_revision": 2},
        )
        assert failed_regen.status_code == 202
        await run_next_attempt(app, settings, httpx.MockTransport(handler))
        failed = await client.get(
            f"/api/v1/workspace/attempts/{failed_regen.json()['attempt']['id']}"
        )
        assert failed.json()["status"] == "failed"
        assert failed.json()["error_category"] == "client_error"
        assert "error_type" not in failed.json()

        detail_after_failure = await client.get(
            f"/api/v1/workspace/conversations/{conversation_id}"
        )
        active_after_failure = [
            message
            for message in detail_after_failure.json()["turns"][0]["assistant_messages"]
            if message["active"]
        ]
        assert [message["content"] for message in active_after_failure] == ["Answer v1"]

        successful_regen = await client.post(
            f"/api/v1/workspace/conversations/{conversation_id}/regenerate",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"expected_revision": 3},
        )
        assert successful_regen.status_code == 202
        await run_next_attempt(app, settings, httpx.MockTransport(handler))

        second_turn = await client.post(
            f"/api/v1/workspace/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"content": "Second question", "expected_revision": 4},
        )
        assert second_turn.status_code == 202
        await run_next_attempt(app, settings, httpx.MockTransport(handler))

        transcript = prompts[-1]
        assert "First question" in transcript
        assert "Answer v2" in transcript
        assert "Answer v1" not in transcript
        assert "Second question" in transcript
        assert 'configured Agent "Workspace Agent" (version 1)' in transcript
        assert "TRANSCRIPT_JSON" not in transcript
        assert transcript.endswith("Assistant:\n")

        stale_turn_regen = await client.post(
            f"/api/v1/workspace/conversations/{conversation_id}/messages/"
            f"{first_user_message_id}/regenerate",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"expected_revision": 5},
        )
        assert stale_turn_regen.status_code == 409
        assert stale_turn_regen.json()["detail"]["code"] == "regeneration_not_latest"

        detail = await client.get(f"/api/v1/workspace/conversations/{conversation_id}")
        assert len(detail.json()["turns"]) == 2
        first_variants = detail.json()["turns"][0]["assistant_messages"]
        assert len(first_variants) == 2
        assert [item["content"] for item in first_variants if item["active"]] == ["Answer v2"]

        async with app.state.session_factory() as session:
            artifact_count = await session.scalar(select(func.count()).select_from(ArtifactIndex))
        assert artifact_count == 4

        small = await client.post(
            "/api/v1/workspace/conversations",
            headers=headers,
            json={"agent_version_id": str(small_version_id), "title": "Too small"},
        )
        too_long = await client.post(
            f"/api/v1/workspace/conversations/{small.json()['id']}/messages",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"content": "Cannot fit", "expected_revision": 0},
        )
        assert too_long.status_code == 422
        assert too_long.json()["detail"]["code"] == "prompt_too_long"
        small_detail = await client.get(
            f"/api/v1/workspace/conversations/{small.json()['id']}"
        )
        assert small_detail.json()["revision"] == 0
        assert small_detail.json()["turns"] == []

        patch_target = await client.post(
            "/api/v1/workspace/conversations",
            headers=headers,
            json={"agent_version_id": str(version_id), "title": "Patch me"},
        )
        patched = await client.patch(
            f"/api/v1/workspace/conversations/{patch_target.json()['id']}",
            headers=headers,
            json={"title": "Renamed", "archived": True, "expected_revision": 0},
        )
        assert patched.json()["title"] == "Renamed"
        assert patched.json()["archived_at"] is not None
        visible = await client.get(
            "/api/v1/workspace/conversations?include_archived=false"
        )
        assert patch_target.json()["id"] not in {
            item["id"] for item in visible.json()["items"]
        }

        async with session_scope(app.state.session_factory) as session:
            old_version = await session.get(AgentVersion, version_id)
            assert old_version is not None
            old_version.lifecycle_status = AgentVersionLifecycle.RETIRED
            new_version = AgentVersion(
                agent_id=old_version.agent_id,
                version=2,
                endpoint_url=old_version.endpoint_url,
                auth_type=old_version.auth_type,
                request_template=old_version.request_template,
                answer_jsonpath=old_version.answer_jsonpath,
                citations_jsonpath=old_version.citations_jsonpath,
                lifecycle_status=AgentVersionLifecycle.ACTIVE,
                concurrency=old_version.concurrency,
                max_prompt_characters=old_version.max_prompt_characters,
            )
            session.add(new_version)
            await session.flush()
            new_version_id = new_version.id

        retired_send = await client.post(
            f"/api/v1/workspace/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"content": "Must repin first", "expected_revision": 5},
        )
        assert retired_send.status_code == 409
        assert retired_send.json()["detail"]["code"] == "agent_version_unavailable"

        mismatched_repin = await client.patch(
            f"/api/v1/workspace/conversations/{conversation_id}",
            headers=headers,
            json={"agent_version_id": str(small_version_id), "expected_revision": 5},
        )
        assert mismatched_repin.status_code == 409
        assert mismatched_repin.json()["detail"]["code"] == "agent_identity_mismatch"

        repinned = await client.patch(
            f"/api/v1/workspace/conversations/{conversation_id}",
            headers=headers,
            json={"agent_version_id": str(new_version_id), "expected_revision": 5},
        )
        assert repinned.status_code == 200
        assert repinned.json()["agent_version_id"] == str(new_version_id)
        assert repinned.json()["revision"] == 6

        repinned_send = await client.post(
            f"/api/v1/workspace/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"content": "Continue after promotion", "expected_revision": 6},
        )
        assert repinned_send.status_code == 202
        assert repinned_send.json()["attempt"]["agent_version_id"] == str(new_version_id)
        active_repin = await client.patch(
            f"/api/v1/workspace/conversations/{conversation_id}",
            headers=headers,
            json={"agent_version_id": str(new_version_id), "expected_revision": 7},
        )
        assert active_repin.status_code == 409
        assert active_repin.json()["detail"]["code"] == "attempt_in_progress"
        await run_next_attempt(app, settings, httpx.MockTransport(handler))
        assert "First question" in prompts[-1]
        assert "Second turn answer" in prompts[-1]
        assert "Continue after promotion" in prompts[-1]

        async with app.state.session_factory() as session:
            artifact_count = await session.scalar(select(func.count()).select_from(ArtifactIndex))
        assert artifact_count == 5

        async with session_scope(app.state.session_factory) as session:
            named = await create_workspace_user(
                session,
                username="workspace.other",
                display_name="Other User",
                role=SessionRole.READ_ONLY,
                password="other-user-secret",
            )
            named_id = named.id
        named_login = await client.post(
            "/api/v1/auth/user-login",
            headers={"Origin": "http://test"},
            json={"username": "workspace.other", "password": "other-user-secret"},
        )
        assert named_login.status_code == 200
        assert named_login.json()["user_id"] == str(named_id)
        assert (await client.get("/api/v1/workspace/conversations")).json() == {
            "items": []
        }
        isolated = await client.get(f"/api/v1/workspace/conversations/{conversation_id}")
        assert isolated.status_code == 404


async def test_workspace_running_cancel_stale_fencing_and_concurrent_claims(
    tmp_path: Path,
) -> None:
    request_started = asyncio.Event()

    async def blocking_handler(_: httpx.Request) -> httpx.Response:
        request_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async with workspace_client(tmp_path) as (client, app, settings, version_id, _):
        headers = await login_operator(client)

        async def queue_conversation(title: str) -> tuple[str, str]:
            conversation = await client.post(
                "/api/v1/workspace/conversations",
                headers=headers,
                json={"agent_version_id": str(version_id), "title": title},
            )
            attempt = await client.post(
                f"/api/v1/workspace/conversations/{conversation.json()['id']}/messages",
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json={"content": title, "expected_revision": 0},
            )
            assert attempt.status_code == 202
            return conversation.json()["id"], attempt.json()["attempt"]["id"]

        _, cancel_attempt_id = await queue_conversation("Cancel running")
        owner_id = "cancel-worker"
        assert await acquire_worker_lease(app.state.engine, owner_id)
        cancel_claim = await claim_next_workspace_attempt(
            app.state.engine,
            owner_id,
            lease_seconds=settings.worker_lease_seconds,
        )
        assert cancel_claim is not None
        task = asyncio.create_task(
            process_workspace_attempt(
                app.state.session_factory,
                settings,
                cancel_claim,
                artifact_store=app.state.artifact_store,
                token_cipher=None,
                owner_id=owner_id,
                limits=WorkerLimits.from_settings(settings),
                transport=httpx.MockTransport(blocking_handler),
            )
        )
        await asyncio.wait_for(request_started.wait(), timeout=1)
        canceled = await client.post(
            f"/api/v1/workspace/attempts/{cancel_attempt_id}/cancel", headers=headers
        )
        assert canceled.json()["cancel_requested_at"] is not None
        assert await asyncio.wait_for(task, timeout=2)
        terminal = await client.get(
            f"/api/v1/workspace/attempts/{cancel_attempt_id}"
        )
        assert terminal.json()["status"] == "canceled"

        _, first_id = await queue_conversation("Concurrent one")
        _, second_id = await queue_conversation("Concurrent two")
        claim_results = await asyncio.gather(
            claim_next_workspace_attempt(
                app.state.engine,
                owner_id,
                lease_seconds=settings.worker_lease_seconds,
            ),
            claim_next_workspace_attempt(
                app.state.engine,
                owner_id,
                lease_seconds=settings.worker_lease_seconds,
            ),
        )
        assert all(claim is not None for claim in claim_results)
        claims = [claim for claim in claim_results if claim is not None]
        assert {str(claim.attempt_id) for claim in claims} == {first_id, second_id}
        assert len({claim.attempt_id for claim in claims}) == 2

        async with session_scope(app.state.session_factory) as session:
            await session.execute(
                update(WorkspaceAttempt)
                .where(WorkspaceAttempt.id.in_([claim.attempt_id for claim in claims]))
                .values(lease_expires_at=func.datetime("now", "-1 second"))
            )
            canceled_stale = await session.get(WorkspaceAttempt, claims[0].attempt_id)
            assert canceled_stale is not None
            canceled_stale.cancel_requested_at = utc_now()
        stale = await fail_stale_workspace_attempts(app.state.engine)
        assert set(stale) == {claim.attempt_id for claim in claims}

        old_claim = WorkspaceClaim(claims[0].attempt_id, claims[0].claim_token)
        provider_calls = 0

        async def forbidden_handler(_: httpx.Request) -> httpx.Response:
            nonlocal provider_calls
            provider_calls += 1
            return httpx.Response(200, json={"answer": "must not run"})

        assert not await process_workspace_attempt(
            app.state.session_factory,
            settings,
            old_claim,
            artifact_store=create_artifact_store(settings),
            token_cipher=None,
            owner_id=owner_id,
            limits=WorkerLimits.from_settings(settings),
            transport=httpx.MockTransport(forbidden_handler),
        )
        assert provider_calls == 0
        async with app.state.session_factory() as session:
            terminal_rows = list(
                await session.execute(
                    select(
                        WorkspaceAttempt.id,
                        WorkspaceAttempt.status,
                        WorkspaceAttempt.error_type,
                        WorkspaceAttempt.error_category,
                    ).where(
                        WorkspaceAttempt.id.in_([claim.attempt_id for claim in claims])
                    )
                )
            )
            active_messages = await session.scalar(
                select(func.count())
                .select_from(WorkspaceMessage)
                .where(WorkspaceMessage.active.is_(True))
            )
            conversations = await session.scalar(
                select(func.count()).select_from(WorkspaceConversation)
            )
        terminal = {row.id: row for row in terminal_rows}
        assert terminal[claims[0].attempt_id].status == WorkspaceAttemptStatus.CANCELED
        assert terminal[claims[0].attempt_id].error_type == "workspace_canceled"
        assert terminal[claims[0].attempt_id].error_category == "canceled"
        assert terminal[claims[1].attempt_id].status == WorkspaceAttemptStatus.FAILED
        assert terminal[claims[1].attempt_id].error_type == "worker_lost"
        assert terminal[claims[1].attempt_id].error_category == "worker_lost"
        assert active_messages == conversations


async def test_workspace_artifact_put_cancellation_deletes_unindexed_object(
    tmp_path: Path,
) -> None:
    class BlockingArtifactStore:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.deleted: list[str] = []

        def put(self, key: str, content: bytes) -> StoredArtifact:
            self.started.set()
            if not self.release.wait(timeout=2):
                raise TimeoutError("test artifact put was not released")
            return StoredArtifact(key, "a" * 64, len(content))

        def read(self, key: str, *, expected_sha256: str, expected_size: int) -> bytes:
            raise AssertionError((key, expected_sha256, expected_size))

        def delete(self, key: str) -> bool:
            self.deleted.append(key)
            return True

        def verify_writable(self) -> None:
            return None

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answer": "Stored response"})

    async with workspace_client(tmp_path) as (client, app, settings, version_id, _):
        headers = await login_operator(client)
        conversation = await client.post(
            "/api/v1/workspace/conversations",
            headers=headers,
            json={"agent_version_id": str(version_id), "title": "Artifact cancellation"},
        )
        queued = await client.post(
            f"/api/v1/workspace/conversations/{conversation.json()['id']}/messages",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"content": "Store this", "expected_revision": 0},
        )
        owner_id = "artifact-worker"
        assert await acquire_worker_lease(app.state.engine, owner_id)
        claim = await claim_next_workspace_attempt(
            app.state.engine,
            owner_id,
            lease_seconds=settings.worker_lease_seconds,
        )
        assert claim is not None
        store = BlockingArtifactStore()
        task = asyncio.create_task(
            process_workspace_attempt(
                app.state.session_factory,
                settings,
                claim,
                artifact_store=store,
                token_cipher=None,
                owner_id=owner_id,
                limits=WorkerLimits.from_settings(settings),
                transport=httpx.MockTransport(handler),
            )
        )
        assert await asyncio.to_thread(store.started.wait, 1)
        task.cancel()
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        expected_key = (
            f"workspace/conversations/{conversation.json()['id']}/attempts/"
            f"{queued.json()['attempt']['id']}/response.json"
        )
        assert store.deleted == [expected_key]
        async with app.state.session_factory() as session:
            artifact_count = await session.scalar(select(func.count()).select_from(ArtifactIndex))
            attempt = await session.get(
                WorkspaceAttempt, UUID(queued.json()["attempt"]["id"])
            )
        assert artifact_count == 0
        assert attempt is not None and attempt.status == WorkspaceAttemptStatus.RUNNING


async def test_workspace_worker_rejects_mismatched_attempt_message_relationship(
    tmp_path: Path,
) -> None:
    provider_calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, json={"answer": "must not execute"})

    async with workspace_client(tmp_path) as (client, app, settings, version_id, _):
        headers = await login_operator(client)
        created = await client.post(
            "/api/v1/workspace/conversations",
            headers=headers,
            json={"agent_version_id": str(version_id), "title": "Relational defense"},
        )
        conversation_id = UUID(created.json()["id"])
        async with session_scope(app.state.session_factory) as session:
            conversation = await session.get(WorkspaceConversation, conversation_id)
            assert conversation is not None
            conversation.last_turn_no = 1
            conversation.revision = 1
            assistant = WorkspaceMessage(
                conversation_id=conversation.id,
                turn_no=1,
                role=WorkspaceMessageRole.ASSISTANT,
                variant_no=1,
                active=True,
                content="Not a user message",
            )
            session.add(assistant)
            await session.flush()
            malformed = WorkspaceAttempt(
                conversation_id=conversation.id,
                user_message_id=assistant.id,
                agent_version_id=version_id,
                generation_no=1,
                idempotency_key=str(uuid4()),
                request_fingerprint="a" * 64,
                status=WorkspaceAttemptStatus.QUEUED,
                prompt_hash="b" * 64,
                prompt_characters=100,
                prompt_format="workspace-transcript-v1",
            )
            session.add(malformed)
            await session.flush()
            malformed_id = malformed.id

        owner_id = "relational-worker"
        assert await acquire_worker_lease(app.state.engine, owner_id)
        claim = await claim_next_workspace_attempt(
            app.state.engine,
            owner_id,
            lease_seconds=settings.worker_lease_seconds,
        )
        assert claim is not None and claim.attempt_id == malformed_id
        assert await process_workspace_attempt(
            app.state.session_factory,
            settings,
            claim,
            artifact_store=app.state.artifact_store,
            token_cipher=None,
            owner_id=owner_id,
            limits=WorkerLimits.from_settings(settings),
            transport=httpx.MockTransport(handler),
        )
        assert provider_calls == 0
        async with app.state.session_factory() as session:
            attempt = await session.get(WorkspaceAttempt, malformed_id)
        assert attempt is not None
        assert attempt.status == WorkspaceAttemptStatus.FAILED
        assert attempt.error_type == "relational_integrity"
        assert attempt.error_category == "internal"
