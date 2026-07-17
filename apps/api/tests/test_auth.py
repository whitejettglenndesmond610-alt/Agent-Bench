import asyncio
import hashlib
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from agent_eval.api.dependencies import OperatorSession
from agent_eval.api.main import create_app
from agent_eval.core.settings import Settings

password_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
READ_ONLY_PASSWORD = "readonly-secret"
OPERATOR_PASSWORD = "operator-secret"


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


def auth_settings(database_path: Path, data_dir: Path, **overrides: object) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        data_dir=data_dir,
        environment="test",
        read_only_password_hash=password_hasher.hash(READ_ONLY_PASSWORD),
        operator_password_hash=password_hasher.hash(OPERATOR_PASSWORD),
        **overrides,
    )


@asynccontextmanager
async def auth_client(
    tmp_path: Path, **settings_overrides: object
) -> AsyncIterator[tuple[AsyncClient, FastAPI, Path]]:
    database_path = tmp_path / "auth.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    app = create_app(auth_settings(database_path, tmp_path, **settings_overrides))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, app, database_path


async def login(
    client: AsyncClient, role: str, password: str, origin: str = "http://test"
) -> Response:
    return await client.post(
        "/api/v1/auth/login",
        headers={"Origin": origin},
        json={"role": role, "password": password},
    )


async def test_login_session_and_csrf_logout(tmp_path: Path) -> None:
    async with auth_client(tmp_path) as (client, _, database_path):
        response = await login(client, "read_only", READ_ONLY_PASSWORD)

        assert response.status_code == 200
        assert response.json()["role"] == "read_only"
        assert any("HttpOnly" in value for value in response.headers.get_list("set-cookie"))

        session_token = client.cookies.get("agent_eval_session")
        csrf_token = client.cookies.get("agent_eval_csrf")
        assert session_token is not None
        assert csrf_token is not None

        with sqlite3.connect(database_path) as connection:
            stored_tokens = connection.execute(
                "SELECT token_hash, csrf_token_hash FROM sessions"
            ).fetchone()

        assert stored_tokens is not None
        stored_token_hash, stored_csrf_hash = stored_tokens
        assert stored_token_hash == hashlib.sha256(session_token.encode()).hexdigest()
        assert stored_csrf_hash == hashlib.sha256(csrf_token.encode()).hexdigest()
        assert stored_token_hash != session_token

        current = await client.get("/api/v1/auth/session")
        assert current.status_code == 200
        assert current.json()["role"] == "read_only"

        missing_csrf = await client.post("/api/v1/auth/logout", headers={"Origin": "http://test"})
        assert missing_csrf.status_code == 403

        logout = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "http://test", "X-CSRF-Token": csrf_token},
        )
        assert logout.status_code == 204
        assert (await client.get("/api/v1/auth/session")).status_code == 401


async def test_operator_dependency_rejects_read_only_session(tmp_path: Path) -> None:
    async with auth_client(tmp_path) as (client, app, _):

        async def operator_only(_: OperatorSession) -> dict[str, bool]:
            return {"allowed": True}

        app.add_api_route("/api/v1/test/operator", operator_only, methods=["GET"])

        assert (await login(client, "read_only", READ_ONLY_PASSWORD)).status_code == 200
        assert (await client.get("/api/v1/test/operator")).status_code == 403

        assert (await login(client, "operator", OPERATOR_PASSWORD)).status_code == 200
        operator_response = await client.get("/api/v1/test/operator")
        assert operator_response.status_code == 200
        assert operator_response.json() == {"allowed": True}


async def test_login_rate_limit_and_origin_validation(tmp_path: Path) -> None:
    async with auth_client(tmp_path, login_max_attempts=2) as (client, _, _):
        assert (await login(client, "operator", "wrong")).status_code == 401
        assert (await login(client, "operator", "wrong-again")).status_code == 401

        limited = await login(client, "operator", OPERATOR_PASSWORD)
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) >= 1

    other_path = tmp_path / "origin"
    other_path.mkdir()
    async with auth_client(other_path) as (client, _, _):
        rejected = await login(
            client, "operator", OPERATOR_PASSWORD, origin="https://untrusted.example"
        )
        assert rejected.status_code == 403
        assert rejected.json()["detail"]["code"] == "origin_mismatch"

    missing_path = tmp_path / "missing-origin"
    missing_path.mkdir()
    async with auth_client(missing_path) as (client, _, _):
        missing_origin = await client.post(
            "/api/v1/auth/login",
            json={"role": "operator", "password": OPERATOR_PASSWORD},
        )
        assert missing_origin.status_code == 403

        wrong_scheme = await login(
            client, "operator", OPERATOR_PASSWORD, origin="https://test"
        )
        assert wrong_scheme.status_code == 403


async def test_validation_errors_do_not_echo_password(tmp_path: Path) -> None:
    sentinel = "password-that-must-never-be-returned"
    async with auth_client(tmp_path) as (client, _, _):
        response = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://test"},
            json={"role": "operator", "password": sentinel * 100},
        )

    assert response.status_code == 422
    assert sentinel.encode() not in response.content
    assert b'"input"' not in response.content


async def test_login_requires_configured_password_hash(tmp_path: Path) -> None:
    database_path = tmp_path / "unconfigured.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await login(client, "operator", OPERATOR_PASSWORD)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "auth_not_configured"
