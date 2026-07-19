import asyncio
import hashlib
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select

from agent_eval.api.dependencies import OperatorSession
from agent_eval.api.main import create_app
from agent_eval.api.routes import auth as auth_routes
from agent_eval.core.settings import Settings
from agent_eval.db.base import utc_now
from agent_eval.db.enums import SessionRole
from agent_eval.db.models import AuthUser, Session
from agent_eval.db.session import session_scope
from agent_eval.maintenance.workspace_users import (
    create_workspace_user,
    reset_workspace_user_password,
)

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
        assert response.json()["is_shared"] is True
        assert response.json()["workspace_enabled"] is True
        assert response.json()["display_name"] == "普通用户"
        assert any("HttpOnly" in value for value in response.headers.get_list("set-cookie"))
        identity_cookie = next(
            value
            for value in response.headers.get_list("set-cookie")
            if value.startswith("agent_eval_workspace_identity=")
        )
        assert "HttpOnly" in identity_cookie
        assert "Max-Age=31536000" in identity_cookie
        assert "Path=/api/v1/auth/login" in identity_cookie
        assert "SameSite=strict" in identity_cookie

        session_token = client.cookies.get("agent_eval_session")
        csrf_token = client.cookies.get("agent_eval_csrf")
        workspace_identity = client.cookies.get("agent_eval_workspace_identity")
        assert session_token is not None
        assert csrf_token is not None
        assert workspace_identity is not None
        first_user_id = response.json()["user_id"]

        with sqlite3.connect(database_path) as connection:
            stored_tokens = connection.execute(
                "SELECT token_hash, csrf_token_hash FROM sessions"
            ).fetchone()
            stored_identity = connection.execute(
                "SELECT subject_key, auth_source FROM auth_users WHERE id = ?",
                (first_user_id.replace("-", ""),),
            ).fetchone()

        assert stored_tokens is not None
        stored_token_hash, stored_csrf_hash = stored_tokens
        assert stored_token_hash == hashlib.sha256(session_token.encode()).hexdigest()
        assert stored_csrf_hash == hashlib.sha256(csrf_token.encode()).hexdigest()
        assert stored_token_hash != session_token
        assert stored_identity is not None
        assert stored_identity[0] == (
            "shared_browser:" + hashlib.sha256(workspace_identity.encode()).hexdigest()
        )
        assert stored_identity[1] == "shared_browser"
        assert workspace_identity not in stored_identity[0]

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
        assert client.cookies.get("agent_eval_workspace_identity") == workspace_identity

        restored = await login(client, "read_only", READ_ONLY_PASSWORD)
        assert restored.status_code == 200
        assert restored.json()["user_id"] == first_user_id


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


async def test_named_user_login_workspace_entitlement_and_disable(tmp_path: Path) -> None:
    async with auth_client(tmp_path) as (client, app, _):
        async with session_scope(app.state.session_factory) as session:
            user = await create_workspace_user(
                session,
                username="Alice.Example",
                display_name="Alice Example",
                role=SessionRole.READ_ONLY,
                password="named-user-secret",
            )
            user_id = user.id

        response = await client.post(
            "/api/v1/auth/user-login",
            headers={"Origin": "http://test"},
            json={"username": "alice.example", "password": "named-user-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "role": "read_only",
            "expires_at": response.json()["expires_at"],
            "user_id": str(user_id),
            "display_name": "Alice Example",
            "is_shared": False,
            "workspace_enabled": True,
        }
        assert "password" not in response.text
        assert "argon2" not in response.text

        catalog = await client.get("/api/v1/workspace/agents")
        assert catalog.status_code == 200

        async with session_scope(app.state.session_factory) as session:
            stored = await session.get(AuthUser, user_id)
            assert stored is not None
            stored.disabled_at = utc_now()

        current = await client.get("/api/v1/auth/session")
        assert current.status_code == 401
        assert current.json()["detail"]["code"] == "user_disabled"

        disabled_login = await client.post(
            "/api/v1/auth/user-login",
            headers={"Origin": "http://test"},
            json={"username": "alice.example", "password": "named-user-secret"},
        )
        assert disabled_login.status_code == 401


async def test_password_reset_revokes_all_named_user_sessions(tmp_path: Path) -> None:
    async with auth_client(tmp_path) as (client, app, _):
        async with session_scope(app.state.session_factory) as session:
            user = await create_workspace_user(
                session,
                username="reset.user",
                display_name="Reset User",
                role=SessionRole.READ_ONLY,
                password="old-secret",
            )
            user_id = user.id

        for _ in range(2):
            response = await client.post(
                "/api/v1/auth/user-login",
                headers={"Origin": "http://test"},
                json={"username": "reset.user", "password": "old-secret"},
            )
            assert response.status_code == 200

        async with app.state.session_factory() as session:
            session_count = await session.scalar(
                select(func.count()).select_from(Session).where(Session.user_id == user_id)
            )
        assert session_count == 2

        async with session_scope(app.state.session_factory) as session:
            await reset_workspace_user_password(
                session, username="reset.user", password="new-secret"
            )

        async with app.state.session_factory() as session:
            session_count = await session.scalar(
                select(func.count()).select_from(Session).where(Session.user_id == user_id)
            )
        assert session_count == 0
        assert (await client.get("/api/v1/auth/session")).status_code == 401
        old_password = await client.post(
            "/api/v1/auth/user-login",
            headers={"Origin": "http://test"},
            json={"username": "reset.user", "password": "old-secret"},
        )
        assert old_password.status_code == 401
        new_password = await client.post(
            "/api/v1/auth/user-login",
            headers={"Origin": "http://test"},
            json={"username": "reset.user", "password": "new-secret"},
        )
        assert new_password.status_code == 200


async def test_unknown_named_login_performs_one_fallback_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    def record_verification(password: str, encoded_hash: str) -> bool:
        calls.append((password, encoded_hash))
        return False

    monkeypatch.setattr(auth_routes, "verify_password", record_verification)
    async with auth_client(tmp_path) as (client, _, _):
        invalid = await client.post(
            "/api/v1/auth/user-login",
            headers={"Origin": "http://test"},
            json={"username": "not valid!", "password": "first-password"},
        )
        unknown = await client.post(
            "/api/v1/auth/user-login",
            headers={"Origin": "http://test"},
            json={"username": "unknown.user", "password": "second-password"},
        )

    assert invalid.status_code == unknown.status_code == 401
    assert [password for password, _ in calls] == ["first-password", "second-password"]
    assert len(calls) == 2
    assert all(encoded_hash.startswith("$argon2") for _, encoded_hash in calls)
