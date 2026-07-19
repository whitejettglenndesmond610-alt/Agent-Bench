import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from agent_eval.agents.configuration import (
    AgentConfigurationUnavailable,
    inference_config_for_version,
)
from agent_eval.api.main import create_app
from agent_eval.auth.encryption import TokenCipher, generate_encryption_key
from agent_eval.core.settings import Settings
from agent_eval.db.base import utc_now
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import (
    AgentStatus,
    AgentVersionLifecycle,
    AuthType,
    PrivateAgentHealth,
    SessionRole,
    WorkspaceAttemptStatus,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    WorkspaceAttempt,
    WorkspaceConversation,
)
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.maintenance.workspace_users import create_workspace_user
from agent_eval.runs.service import RunConfigurationError, prepare_evaluation_run
from agent_eval.worker.limits import WorkerLimits
from agent_eval.workspace.executor import process_workspace_attempt
from agent_eval.workspace.queue import WorkspaceClaim

password_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
ENCRYPTION_KEY = generate_encryption_key()
OWNER_PASSWORD = "owner-secret"
OTHER_PASSWORD = "other-secret"
API_KEY = "sk-private-test-key"


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


@asynccontextmanager
async def private_agent_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, FastAPI, Settings, UUID, UUID, UUID]]:
    database_path = tmp_path / "private-agents.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        data_dir=tmp_path,
        environment="test",
        read_only_password_hash=password_hasher.hash("shared-secret"),
        operator_password_hash=password_hasher.hash("shared-operator-secret"),
        encryption_key=ENCRYPTION_KEY,
    )
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    try:
        async with session_scope(sessions) as session:
            owner = await create_workspace_user(
                session,
                username="private.owner",
                display_name="Private Owner",
                role=SessionRole.OPERATOR,
                password=OWNER_PASSWORD,
            )
            other = await create_workspace_user(
                session,
                username="private.other",
                display_name="Other Owner",
                role=SessionRole.READ_ONLY,
                password=OTHER_PASSWORD,
            )
            public_agent = Agent(name="Public Agent", provider="Public Provider")
            session.add(public_agent)
            await session.flush()
            public_version = AgentVersion(
                agent_id=public_agent.id,
                version=1,
                endpoint_url="https://public.example/chat",
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
                lifecycle_status=AgentVersionLifecycle.ACTIVE,
                enabled=True,
            )
            session.add(public_version)
            await session.flush()
            owner_id = owner.id
            other_id = other.id
            public_version_id = public_version.id
    finally:
        await engine.dispose()

    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client, app, settings, owner_id, other_id, public_version_id


async def login_named(client: AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/user-login",
        headers={"Origin": "http://test"},
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    csrf = client.cookies.get("agent_eval_csrf")
    assert csrf is not None
    return {"Origin": "http://test", "X-CSRF-Token": csrf}


def assert_secret_free(response: httpx.Response, *secrets: str) -> None:
    body = response.text
    for secret in secrets:
        assert secret not in body
    for forbidden in (
        "endpoint_url",
        "request_template",
        "answer_jsonpath",
        "encrypted_token",
        "Authorization",
        "raw_response",
        "request_body",
    ):
        assert forbidden not in body


async def mark_attempt_running(
    app: FastAPI, attempt_id: UUID, owner_id: str
) -> WorkspaceClaim:
    token = uuid4().hex
    async with session_scope(app.state.session_factory) as session:
        attempt = await session.get(WorkspaceAttempt, attempt_id)
        assert attempt is not None
        attempt.status = WorkspaceAttemptStatus.RUNNING
        attempt.claim_owner_id = owner_id
        attempt.claim_token = token
        attempt.claimed_at = utc_now()
        attempt.started_at = utc_now()
        attempt.heartbeat_at = utc_now()
        attempt.lease_expires_at = utc_now() + timedelta(minutes=5)
    return WorkspaceClaim(attempt_id=attempt_id, claim_token=token)


async def test_private_agent_api_lifecycle_workspace_and_isolation(tmp_path: Path) -> None:
    async with private_agent_client(tmp_path) as (
        client,
        app,
        settings,
        owner_id,
        other_id,
        public_version_id,
    ):
        headers = await login_named(client, "private.owner", OWNER_PASSWORD)
        response_status = 200
        expected_model = "private-model-v1"
        outbound_calls = 0

        async def provider(request: httpx.Request) -> httpx.Response:
            nonlocal outbound_calls
            outbound_calls += 1
            assert str(request.url) == "https://api.openai.com/v1/chat/completions"
            assert request.headers["Authorization"] == f"Bearer {API_KEY}"
            body = json.loads(request.content)
            assert body == {
                "model": expected_model,
                "messages": [{"role": "user", "content": body["messages"][0]["content"]}],
                "stream": False,
            }
            if response_status != 200:
                return httpx.Response(response_status, json={"error": {"message": API_KEY}})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "OK"}}]},
            )

        app.state.agent_http_transport = httpx.MockTransport(provider)
        created = await client.post(
            "/api/v1/workspace/private-agents",
            headers=headers,
            json={
                "name": "My Private Agent",
                "description": "Owner only",
                "preset": "openai",
                "model": expected_model,
                "api_key": API_KEY,
            },
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "private, no-store"
        assert_secret_free(created, API_KEY, "https://api.openai.com")
        created_body = created.json()
        assert created_body["version"] == 1
        assert created_body["health_status"] == "untested"
        assert created_body["active_agent_version_id"] is None
        assert created_body["workspace_available"] is False
        assert created_body["provider"] == "OpenAI"
        private_agent_id = UUID(created_body["agent_id"])
        version_one_id = UUID(created_body["agent_version_id"])

        async with session_scope(app.state.session_factory) as session:
            stored_one = await session.get(AgentVersion, version_one_id)
            assert stored_one is not None
            assert stored_one.encrypted_token is not None
            assert stored_one.encrypted_token.startswith("v2:")
            assert API_KEY not in stored_one.encrypted_token
            ciphertext_one = stored_one.encrypted_token

        listed = await client.get("/api/v1/workspace/private-agents")
        assert listed.status_code == 200
        assert listed.headers["cache-control"] == "private, no-store"
        assert [item["agent_id"] for item in listed.json()["items"]] == [
            str(private_agent_id)
        ]
        assert_secret_free(listed, API_KEY, ciphertext_one, "https://api.openai.com")

        public_catalog = await client.get("/api/v1/workspace/agents")
        assert {item["visibility"] for item in public_catalog.json()} == {"public"}
        assert str(private_agent_id) not in public_catalog.text
        options_before_test = await client.get("/api/v1/workspace/agent-options")
        assert str(private_agent_id) not in options_before_test.text

        tested = await client.post(
            f"/api/v1/workspace/private-agents/{private_agent_id}/test",
            headers=headers,
        )
        assert tested.status_code == 200
        assert tested.json()["success"] is True
        assert tested.json()["health_status"] == "healthy"
        assert tested.json()["status_code"] == 200
        assert tested.json()["error_code"] is None
        assert tested.json()["active_agent_version_id"] == str(version_one_id)
        assert tested.json()["workspace_available"] is True
        assert_secret_free(tested, API_KEY, "https://api.openai.com", "OK")

        options = await client.get("/api/v1/workspace/agent-options")
        private_option = next(
            item for item in options.json() if item["agent_id"] == str(private_agent_id)
        )
        assert private_option["visibility"] == "private"
        assert private_option["model"] == expected_model
        assert private_option["is_favorite"] is False
        assert str(private_agent_id) not in (await client.get("/api/v1/workspace/agents")).text
        operator_agents = await client.get("/api/v1/agents")
        assert operator_agents.status_code == 200
        assert str(private_agent_id) not in operator_agents.text
        global_leaderboard = await client.get("/api/v1/leaderboards/global/overall")
        assert global_leaderboard.status_code == 200
        assert str(private_agent_id) not in global_leaderboard.text
        private_leaderboard_detail = await client.get(
            f"/api/v1/leaderboards/agents/{private_agent_id}"
        )
        assert private_leaderboard_detail.status_code == 404
        agent_statuses = await client.get("/api/v1/evaluation-runs/agent-statuses")
        assert agent_statuses.status_code == 200
        assert str(private_agent_id) not in agent_statuses.text
        history = await client.get("/api/v1/leaderboards/history")
        assert history.status_code == 200
        assert str(private_agent_id) not in history.text

        favorite = await client.put(
            f"/api/v1/workspace/agents/{private_agent_id}/favorite", headers=headers
        )
        assert favorite.status_code == 404

        conversation = await client.post(
            "/api/v1/workspace/conversations",
            headers=headers,
            json={"agent_version_id": str(version_one_id), "title": "Private chat"},
        )
        assert conversation.status_code == 201
        assert conversation.json()["agent"]["visibility"] == "private"
        assert conversation.json()["agent"]["model"] == expected_model
        conversation_id = conversation.json()["id"]

        comparison = await client.post(
            "/api/v1/workspace/comparisons",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "prompt": "Compare this",
                "agent_version_ids": [str(public_version_id), str(version_one_id)],
            },
        )
        assert comparison.status_code == 202
        assert {item["agent"]["visibility"] for item in comparison.json()["items"]} == {
            "public",
            "private",
        }
        private_comparison_attempt_id = UUID(
            next(
                item
                for item in comparison.json()["items"]
                if item["agent"]["visibility"] == "private"
            )["attempt"]["id"]
        )

        metadata_patch = await client.patch(
            f"/api/v1/workspace/private-agents/{private_agent_id}",
            headers=headers,
            json={"expected_version": 1, "description": None},
        )
        assert metadata_patch.status_code == 200
        assert metadata_patch.json()["version"] == 1
        assert metadata_patch.json()["description"] is None

        expected_model = "private-model-v2"
        config_patch = await client.patch(
            f"/api/v1/workspace/private-agents/{private_agent_id}",
            headers=headers,
            json={"expected_version": 1, "model": expected_model},
        )
        assert config_patch.status_code == 200
        assert config_patch.json()["version"] == 2
        assert config_patch.json()["health_status"] == "untested"
        assert config_patch.json()["active_agent_version_id"] == str(version_one_id)
        assert config_patch.json()["active_model"] == "private-model-v1"
        version_two_id = UUID(config_patch.json()["agent_version_id"])
        assert_secret_free(config_patch, API_KEY, "https://api.openai.com")

        cipher = TokenCipher.from_secret(settings.encryption_key)  # type: ignore[arg-type]
        async with session_scope(app.state.session_factory) as session:
            stored_two = await session.get(AgentVersion, version_two_id)
            assert stored_two is not None and stored_two.encrypted_token is not None
            assert (
                cipher.decrypt_private(
                    stored_two.encrypted_token, owner_id, version_two_id
                )
                == API_KEY
            )
            with pytest.raises(InvalidTag):
                cipher.decrypt_private(stored_two.encrypted_token, other_id, version_two_id)
            with pytest.raises(InvalidTag):
                cipher.decrypt_private(stored_two.encrypted_token, owner_id, version_one_id)

        old_option = next(
            item
            for item in (await client.get("/api/v1/workspace/agent-options")).json()
            if item["agent_id"] == str(private_agent_id)
        )
        assert old_option["agent_version_id"] == str(version_one_id)

        stale = await client.patch(
            f"/api/v1/workspace/private-agents/{private_agent_id}",
            headers=headers,
            json={"expected_version": 1, "name": "Stale"},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "version_conflict"

        response_status = 401
        failed_test = await client.post(
            f"/api/v1/workspace/private-agents/{private_agent_id}/test",
            headers=headers,
        )
        assert failed_test.status_code == 200
        assert failed_test.json()["success"] is False
        assert failed_test.json()["error_code"] == "authentication_failed"
        assert failed_test.json()["health_status"] == "unhealthy"
        assert_secret_free(failed_test, API_KEY)
        assert next(
            item
            for item in (await client.get("/api/v1/workspace/agent-options")).json()
            if item["agent_id"] == str(private_agent_id)
        )["agent_version_id"] == str(version_one_id)

        response_status = 200
        promoted = await client.post(
            f"/api/v1/workspace/private-agents/{private_agent_id}/test",
            headers=headers,
        )
        assert promoted.json()["success"] is True
        assert promoted.json()["agent_version_id"] == str(version_two_id)
        async with session_scope(app.state.session_factory) as session:
            old = await session.get(AgentVersion, version_one_id)
            new = await session.get(AgentVersion, version_two_id)
            assert old is not None and new is not None
            assert old.lifecycle_status == AgentVersionLifecycle.RETIRED
            assert old.enabled is False
            assert new.lifecycle_status == AgentVersionLifecycle.ACTIVE
            assert new.private_health_status == PrivateAgentHealth.HEALTHY

        repinned = await client.patch(
            f"/api/v1/workspace/conversations/{conversation_id}",
            headers=headers,
            json={"agent_version_id": str(version_two_id), "expected_revision": 0},
        )
        assert repinned.status_code == 200
        assert repinned.json()["revision"] == 1

        sent = await client.post(
            f"/api/v1/workspace/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"content": "Hello", "expected_revision": 1},
        )
        assert sent.status_code == 202
        attempt_id = UUID(sent.json()["attempt"]["id"])
        claim = await mark_attempt_running(app, attempt_id, "private-worker")
        processed = await process_workspace_attempt(
            app.state.session_factory,
            settings,
            claim,
            artifact_store=app.state.artifact_store,
            token_cipher=app.state.token_cipher,
            owner_id="private-worker",
            limits=WorkerLimits.from_settings(settings),
            transport=httpx.MockTransport(provider),
        )
        assert processed
        async with session_scope(app.state.session_factory) as session:
            attempt = await session.get(WorkspaceAttempt, attempt_id)
            assert attempt is not None
            assert attempt.status == WorkspaceAttemptStatus.SUCCEEDED

        health_conversation = await client.post(
            "/api/v1/workspace/conversations",
            headers=headers,
            json={"agent_version_id": str(version_two_id), "title": "Health guard"},
        )
        health_sent = await client.post(
            f"/api/v1/workspace/conversations/{health_conversation.json()['id']}/messages",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"content": "Check balance", "expected_revision": 0},
        )
        health_attempt_id = UUID(health_sent.json()["attempt"]["id"])
        health_claim = await mark_attempt_running(
            app, health_attempt_id, "private-health-worker"
        )
        response_status = 402
        assert await process_workspace_attempt(
            app.state.session_factory,
            settings,
            health_claim,
            artifact_store=app.state.artifact_store,
            token_cipher=app.state.token_cipher,
            owner_id="private-health-worker",
            limits=WorkerLimits.from_settings(settings),
            transport=httpx.MockTransport(provider),
        )
        async with session_scope(app.state.session_factory) as session:
            health_attempt = await session.get(WorkspaceAttempt, health_attempt_id)
            guarded_version = await session.get(AgentVersion, version_two_id)
            assert health_attempt is not None
            assert health_attempt.status == WorkspaceAttemptStatus.FAILED
            assert guarded_version is not None
            assert guarded_version.enabled is False
            assert guarded_version.private_health_status == PrivateAgentHealth.UNHEALTHY
            assert guarded_version.private_health_error_code == "balance_exhausted"

        response_status = 200
        restored = await client.post(
            f"/api/v1/workspace/private-agents/{private_agent_id}/test",
            headers=headers,
        )
        assert restored.status_code == 200
        assert restored.json()["success"] is True
        assert restored.json()["workspace_available"] is True

        draft = await client.post(
            "/api/v1/evaluation-cases",
            headers=headers,
            json={"source_attempt_id": str(attempt_id), "idempotency_key": str(uuid4())},
        )
        assert draft.status_code == 409
        assert draft.json()["detail"]["code"] == "private_agent_not_allowed"

        with pytest.raises(RunConfigurationError) as formal_error:
            async with app.state.session_factory() as session:
                await prepare_evaluation_run(
                    session,
                    name="Forbidden private run",
                    agent_version_ids=[version_two_id],
                    dataset_version_ids=[],
                    judge_config_id=uuid4(),
                    score_config_id=uuid4(),
                )
        assert formal_error.value.code == "private_agent_not_allowed"

        other_client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )
        try:
            other_headers = await login_named(other_client, "private.other", OTHER_PASSWORD)
            assert (await other_client.get("/api/v1/workspace/private-agents")).json() == {
                "items": []
            }
            cross_owner = await other_client.patch(
                f"/api/v1/workspace/private-agents/{private_agent_id}",
                headers=other_headers,
                json={"expected_version": 2, "name": "Not mine"},
            )
            assert cross_owner.status_code == 404
            cross_owner_test = await other_client.post(
                f"/api/v1/workspace/private-agents/{private_agent_id}/test",
                headers=other_headers,
            )
            assert cross_owner_test.status_code == 404
            cross_owner_delete = await other_client.request(
                "DELETE",
                f"/api/v1/workspace/private-agents/{private_agent_id}",
                headers=other_headers,
                json={"expected_version": 2},
            )
            assert cross_owner_delete.status_code == 404
            assert str(private_agent_id) not in (
                await other_client.get("/api/v1/workspace/agent-options")
            ).text
        finally:
            await other_client.aclose()

        sent_second = await client.post(
            f"/api/v1/workspace/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"content": "Cross-owner corruption", "expected_revision": 2},
        )
        assert sent_second.status_code == 202
        corrupt_attempt_id = UUID(sent_second.json()["attempt"]["id"])
        async with session_scope(app.state.session_factory) as session:
            stored_conversation = await session.get(
                WorkspaceConversation, UUID(conversation_id)
            )
            assert stored_conversation is not None
            stored_conversation.owner_user_id = other_id
        corrupt_claim = await mark_attempt_running(
            app, corrupt_attempt_id, "corrupt-private-worker"
        )
        calls_before_corruption = outbound_calls
        assert await process_workspace_attempt(
            app.state.session_factory,
            settings,
            corrupt_claim,
            artifact_store=app.state.artifact_store,
            token_cipher=app.state.token_cipher,
            owner_id="corrupt-private-worker",
            limits=WorkerLimits.from_settings(settings),
            transport=httpx.MockTransport(provider),
        )
        assert outbound_calls == calls_before_corruption
        async with session_scope(app.state.session_factory) as session:
            corrupt_attempt = await session.get(WorkspaceAttempt, corrupt_attempt_id)
            assert corrupt_attempt is not None
            assert corrupt_attempt.status == WorkspaceAttemptStatus.FAILED
            assert corrupt_attempt.error_category == "configuration"
            stored_conversation = await session.get(
                WorkspaceConversation, UUID(conversation_id)
            )
            assert stored_conversation is not None
            stored_conversation.owner_user_id = owner_id

        deleted = await client.request(
            "DELETE",
            f"/api/v1/workspace/private-agents/{private_agent_id}",
            headers=headers,
            json={"expected_version": 2},
        )
        assert deleted.status_code == 204
        assert deleted.headers["cache-control"] == "private, no-store"
        assert (await client.get("/api/v1/workspace/private-agents")).json() == {
            "items": []
        }
        async with session_scope(app.state.session_factory) as session:
            agent = await session.get(Agent, private_agent_id)
            versions = list(
                await session.scalars(
                    select(AgentVersion).where(AgentVersion.agent_id == private_agent_id)
                )
            )
            assert agent is not None and agent.status == AgentStatus.DISABLED
            assert all(version.encrypted_token is None for version in versions)
            assert all(
                version.private_health_status == PrivateAgentHealth.REVOKED
                for version in versions
            )
            canceled_attempt = await session.get(
                WorkspaceAttempt, private_comparison_attempt_id
            )
            assert canceled_attempt is not None
            assert canceled_attempt.status == WorkspaceAttemptStatus.CANCELED

        recreated = await client.post(
            "/api/v1/workspace/private-agents",
            headers=headers,
            json={
                "name": "My Private Agent",
                "preset": "openai",
                "model": "replacement-model",
                "api_key": API_KEY,
            },
        )
        assert recreated.status_code == 201


async def test_shared_browser_can_create_private_agent(tmp_path: Path) -> None:
    async with private_agent_client(tmp_path) as (client, app, _, _, _, _):
        login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://test"},
            json={"role": "read_only", "password": "shared-secret"},
        )
        assert login.status_code == 200
        csrf = client.cookies.get("agent_eval_csrf")
        assert csrf is not None
        created = await client.post(
            "/api/v1/workspace/private-agents",
            headers={"Origin": "http://test", "X-CSRF-Token": csrf},
            json={
                "name": "Shared Browser Private",
                "preset": "deepseek",
                "model": "DeepSeek V4 Pro",
                "api_key": API_KEY,
            },
        )
        assert created.status_code == 201
        assert created.json()["model"] == "deepseek-v4-pro"
        assert created.headers["cache-control"] == "private, no-store"
        assert_secret_free(created, API_KEY, "https://api.deepseek.com")
        assert app.state.token_cipher is not None
        strict = await client.post(
            "/api/v1/workspace/private-agents",
            headers={"Origin": "http://test", "X-CSRF-Token": csrf},
            json={
                "name": "Arbitrary endpoint",
                "preset": "deepseek",
                "model": "deepseek-chat",
                "api_key": API_KEY,
                "endpoint_url": "https://attacker.invalid",
            },
        )
        assert strict.status_code == 422
        assert strict.headers["cache-control"] == "private, no-store"
        blank_key = await client.post(
            "/api/v1/workspace/private-agents",
            headers={"Origin": "http://test", "X-CSRF-Token": csrf},
            json={
                "name": "Blank key",
                "preset": "deepseek",
                "model": "deepseek-chat",
                "api_key": "   ",
            },
        )
        assert blank_key.status_code == 422
        for index in range(2, 11):
            additional = await client.post(
                "/api/v1/workspace/private-agents",
                headers={"Origin": "http://test", "X-CSRF-Token": csrf},
                json={
                    "name": f"Shared Browser Private {index}",
                    "preset": "deepseek",
                    "model": "deepseek-chat",
                    "api_key": API_KEY,
                },
            )
            assert additional.status_code == 201
        limited = await client.post(
            "/api/v1/workspace/private-agents",
            headers={"Origin": "http://test", "X-CSRF-Token": csrf},
            json={
                "name": "Over limit",
                "preset": "deepseek",
                "model": "deepseek-chat",
                "api_key": API_KEY,
            },
        )
        assert limited.status_code == 409
        assert limited.json()["detail"]["code"] == "private_agent_limit_reached"


def test_private_token_aad_and_public_v1_are_separate() -> None:
    cipher = TokenCipher.from_secret(Settings(encryption_key=ENCRYPTION_KEY).encryption_key)  # type: ignore[arg-type]
    owner_id = uuid4()
    version_id = uuid4()
    public = cipher.encrypt("public-token")
    private = cipher.encrypt_private("private-token", owner_id, version_id)

    assert public.startswith("v1:")
    assert private.startswith("v2:")
    assert cipher.decrypt(public) == "public-token"
    assert cipher.decrypt_private(private, owner_id, version_id) == "private-token"
    with pytest.raises(ValueError, match="private token version"):
        cipher.decrypt_private(public, owner_id, version_id)
    with pytest.raises(ValueError, match="encrypted token version"):
        cipher.decrypt(private)

    agent = Agent(id=uuid4(), owner_user_id=owner_id, name="Private", provider="OpenAI")
    version = AgentVersion(
        id=version_id,
        agent=agent,
        agent_id=agent.id,
        version=1,
        endpoint_url="https://api.openai.com/v1/chat/completions",
        auth_type=AuthType.BEARER,
        encrypted_token=private,
        request_template='{"model":"m","messages":[{"role":"user","content":"{{prompt}}"}]}',
        answer_jsonpath="$.choices[0].message.content",
        timeout_seconds=120,
        concurrency=1,
        private_model="m",
        private_health_status=PrivateAgentHealth.HEALTHY,
    )
    assert inference_config_for_version(version, cipher, owner_id).token == "private-token"
    with pytest.raises(AgentConfigurationUnavailable):
        inference_config_for_version(version, cipher, uuid4())


def test_private_agent_migration_preserves_public_rows_and_refuses_private_downgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "private-agent-migration.db"
    config = migration_config(database_path)
    command.upgrade(config, "2c7f9a1d4e63")
    agent_id = uuid4().hex
    version_id = uuid4().hex
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO agents (name, provider, status, id, created_at, updated_at) "
            "VALUES ('Preserved Public', 'Provider', 'enabled', ?, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)",
            (agent_id,),
        )
        connection.execute(
            "INSERT INTO agent_versions (agent_id, version, endpoint_url, auth_type, "
            "request_template, answer_jsonpath, timeout_seconds, concurrency, lifecycle_status, "
            "preflight_status, enabled, id, created_at, updated_at) VALUES "
            "(?, 1, 'https://public.invalid', 'none', '{}', '$.answer', 120, 1, 'active', "
            "'not_run', 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (agent_id, version_id),
        )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT owner_user_id FROM agents WHERE id = ?", (agent_id,)
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT private_health_status FROM agent_versions WHERE id = ?", (version_id,)
        ).fetchone() == ("not_applicable",)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO agents (name, provider, status, id, created_at, updated_at) "
                "VALUES ('Preserved Public', 'Other', 'enabled', ?, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)",
                (uuid4().hex,),
            )

    command.downgrade(config, "2c7f9a1d4e63")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT name FROM agents WHERE id = ?", (agent_id,)
        ).fetchone() == ("Preserved Public",)

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        owner_id = connection.execute(
            "SELECT id FROM auth_users ORDER BY id LIMIT 1"
        ).fetchone()
        assert owner_id is not None
        connection.execute(
            "INSERT INTO agents (name, owner_user_id, provider, status, id, created_at, "
            "updated_at) VALUES ('Private downgrade blocker', ?, 'OpenAI', 'enabled', ?, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (owner_id[0], uuid4().hex),
        )
    with pytest.raises(RuntimeError, match="private Agents remain"):
        command.downgrade(config, "2c7f9a1d4e63")


def test_private_model_alias_migration_updates_model_and_request_template(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "private-model-alias.db"
    config = migration_config(database_path)
    command.upgrade(config, "7a4e1c9b2d60")
    agent_id = uuid4().hex
    version_id = uuid4().hex
    with sqlite3.connect(database_path) as connection:
        owner_id = connection.execute(
            "SELECT id FROM auth_users ORDER BY id LIMIT 1"
        ).fetchone()
        assert owner_id is not None
        connection.execute(
            "INSERT INTO agents (name, owner_user_id, provider, status, id, created_at, "
            "updated_at) VALUES ('Alias Private', ?, 'DeepSeek', 'enabled', ?, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (owner_id[0], agent_id),
        )
        connection.execute(
            "INSERT INTO agent_versions (agent_id, version, endpoint_url, auth_type, "
            "encrypted_token, request_template, answer_jsonpath, citations_jsonpath, "
            "timeout_seconds, concurrency, private_preset, private_model, "
            "private_health_status, private_health_checked_at, private_health_error_code, "
            "private_health_latency_ms, lifecycle_status, preflight_status, enabled, id, "
            "created_at, updated_at) VALUES (?, 1, 'https://api.deepseek.com/chat/completions', "
            "'bearer', 'v2:test', ?, '$.choices[0].message.content', NULL, 120, 1, "
            "'deepseek', 'DeepSeek V4 Pro', 'unhealthy', CURRENT_TIMESTAMP, "
            "'request_rejected', 108, 'draft', 'not_run', 0, ?, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)",
            (
                agent_id,
                '{"model":"DeepSeek V4 Pro","messages":[{"role":"user",'
                '"content":"{{prompt}}"}],"stream":false}',
                version_id,
            ),
        )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        model, template = connection.execute(
            "SELECT private_model, request_template FROM agent_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        assert model == "deepseek-v4-pro"
        assert json.loads(template)["model"] == "deepseek-v4-pro"
