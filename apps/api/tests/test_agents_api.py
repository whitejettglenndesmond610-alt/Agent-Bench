import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_eval.api.main import create_app
from agent_eval.auth.encryption import generate_encryption_key
from agent_eval.core.settings import Settings

OPERATOR_PASSWORD = "operator-secret"
password_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


@asynccontextmanager
async def agent_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, FastAPI, Path]]:
    database_path = tmp_path / "agents.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        data_dir=tmp_path,
        environment="test",
        operator_password_hash=password_hasher.hash(OPERATOR_PASSWORD),
        encryption_key=generate_encryption_key(),
    )
    app = create_app(settings)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client, app, database_path


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


async def test_agent_crud_encrypts_token_and_tests_connections(tmp_path: Path) -> None:
    requests: list[dict[str, object]] = []

    async def agent_handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "url": str(request.url),
                "authorization": request.headers.get("Authorization"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(
            200,
            json={
                "result": {
                    "answer": "Agent answer",
                    "sources": ["https://example.com/reference"],
                }
            },
        )

    async with agent_client(tmp_path) as (client, app, database_path):
        app.state.agent_http_transport = httpx.MockTransport(agent_handler)
        mutation_headers = await login_operator(client)

        create_response = await client.post(
            "/api/v1/agents",
            headers=mutation_headers,
            json={"name": "Demo Agent", "provider": "Demo Provider"},
        )
        assert create_response.status_code == 201
        agent_id = create_response.json()["id"]

        duplicate_response = await client.post(
            "/api/v1/agents",
            headers=mutation_headers,
            json={"name": "Demo Agent", "provider": "Another Provider"},
        )
        assert duplicate_response.status_code == 409

        unsaved_test = await client.post(
            "/api/v1/agents/test-connection",
            headers=mutation_headers,
            json={
                "endpoint_url": "https://agent.example/unsaved",
                "auth_type": "none",
                "request_template": '{"query":"{{prompt}}"}',
                "answer_jsonpath": "$.result.answer",
                "citations_jsonpath": "$.result.sources",
                "prompt": "Unsaved prompt",
            },
        )
        assert unsaved_test.status_code == 200
        assert unsaved_test.json()["success"] is True

        version_response = await client.post(
            f"/api/v1/agents/{agent_id}/versions",
            headers=mutation_headers,
            json={
                "endpoint_url": "https://agent.example/saved",
                "auth_type": "bearer",
                "token": "plain-agent-secret",
                "request_template": '{"query":"{{prompt}}"}',
                "answer_jsonpath": "$.result.answer",
                "citations_jsonpath": "$.result.sources",
                "timeout_seconds": 30,
                "concurrency": 3,
                "max_prompt_characters": 4000,
            },
        )
        assert version_response.status_code == 201
        version = version_response.json()
        version_id = version["id"]
        assert version["version"] == 1
        assert version["token_configured"] is True
        assert version["max_prompt_characters"] == 4000
        assert version["lifecycle_status"] == "draft"
        assert version["preflight_status"] == "not_run"
        assert "token" not in version
        assert "encrypted_token" not in version

        with sqlite3.connect(database_path) as connection:
            encrypted_token = connection.execute(
                "SELECT encrypted_token FROM agent_versions WHERE id = ?",
                (version_id.replace("-", ""),),
            ).fetchone()[0]
        assert encrypted_token.startswith("v1:")
        assert "plain-agent-secret" not in encrypted_token

        detail_response = await client.get(f"/api/v1/agents/{agent_id}")
        assert detail_response.status_code == 200
        assert detail_response.json()["versions"][0]["token_configured"] is True

        premature_promotion = await client.post(
            f"/api/v1/agent-versions/{version_id}/promote",
            headers=mutation_headers,
        )
        assert premature_promotion.status_code == 409
        assert premature_promotion.json()["detail"]["code"] == "preflight_required"

        preflight = await client.post(
            f"/api/v1/agent-versions/{version_id}/preflight",
            headers=mutation_headers,
        )
        assert preflight.status_code == 200
        assert preflight.json()["preflight_status"] == "passed"
        assert preflight.json()["lifecycle_status"] == "verified"
        assert preflight.json()["checks"][0]["key"] == "basic"

        promotion = await client.post(
            f"/api/v1/agent-versions/{version_id}/promote",
            headers=mutation_headers,
        )
        assert promotion.status_code == 200
        assert promotion.json()["lifecycle_status"] == "active"

        saved_test = await client.post(
            f"/api/v1/agent-versions/{version_id}/test",
            headers=mutation_headers,
            json={"prompt": "Saved prompt"},
        )
        assert saved_test.status_code == 200
        assert saved_test.json()["answer"] == "Agent answer"
        assert saved_test.json()["citations"] == ["https://example.com/reference"]

        disable_response = await client.post(
            f"/api/v1/agent-versions/{version_id}/disable",
            headers=mutation_headers,
        )
        assert disable_response.status_code == 200
        assert disable_response.json()["enabled"] is False
        assert disable_response.json()["lifecycle_status"] == "retired"

        list_response = await client.get("/api/v1/agents")
        assert list_response.status_code == 200
        assert [agent["name"] for agent in list_response.json()] == ["Demo Agent"]

    assert requests == [
        {
            "url": "https://agent.example/unsaved",
            "authorization": None,
            "body": {"query": "Unsaved prompt"},
        },
        {
            "url": "https://agent.example/saved",
            "authorization": "Bearer plain-agent-secret",
            "body": {"query": "请只回答：OK"},
        },
        {
            "url": "https://agent.example/saved",
            "authorization": "Bearer plain-agent-secret",
            "body": {"query": "Saved prompt"},
        },
    ]


async def test_agent_validation_errors_do_not_echo_token(tmp_path: Path) -> None:
    sentinel = 'token-"-that-must-not-leak'
    async with agent_client(tmp_path) as (client, _, _):
        mutation_headers = await login_operator(client)
        response = await client.post(
            "/api/v1/agents/test-connection",
            headers=mutation_headers,
            json={
                "endpoint_url": "https://agent.example/run",
                "auth_type": "none",
                "token": sentinel,
                "request_template": '{"query":"{{prompt}}"}',
                "answer_jsonpath": "$.answer",
                "prompt": "test",
            },
        )

    assert response.status_code == 422
    assert sentinel.encode() not in response.content
    assert b'"input"' not in response.content
