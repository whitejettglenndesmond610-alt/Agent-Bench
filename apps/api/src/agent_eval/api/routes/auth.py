import asyncio
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import SecretStr

from agent_eval.api.dependencies import CsrfSession, CurrentSession, DatabaseSession
from agent_eval.api.schemas.auth import LoginRequest, SessionResponse
from agent_eval.auth.rate_limit import LoginRateLimiter
from agent_eval.auth.security import (
    client_ip,
    generate_token,
    hash_token,
    require_same_origin,
    verify_password,
)
from agent_eval.core.settings import Settings
from agent_eval.db.base import utc_now
from agent_eval.db.enums import SessionRole
from agent_eval.db.models import Session

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def password_hash_for_role(settings: Settings, role: SessionRole) -> SecretStr | None:
    if role == SessionRole.OPERATOR:
        return settings.operator_password_hash
    return settings.read_only_password_hash


def session_lifetime(settings: Settings, role: SessionRole) -> timedelta:
    hours = (
        settings.operator_session_hours
        if role == SessionRole.OPERATOR
        else settings.read_only_session_hours
    )
    return timedelta(hours=hours)


def set_auth_cookies(
    response: Response,
    settings: Settings,
    session_token: str,
    csrf_token: str,
    max_age: int,
) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        session_token,
        httponly=True,
        max_age=max_age,
        path="/",
        secure=settings.secure_cookies,
        samesite="lax",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        httponly=False,
        max_age=max_age,
        path="/",
        secure=settings.secure_cookies,
        samesite="lax",
    )


def clear_auth_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


@router.post("/login", response_model=SessionResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    database: DatabaseSession,
) -> SessionResponse:
    require_same_origin(request)
    settings: Settings = request.app.state.settings
    rate_limiter: LoginRateLimiter = request.app.state.login_rate_limiter
    limiter_key = client_ip(request)

    if retry_after := rate_limiter.retry_after(limiter_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
            detail={"code": "login_rate_limited", "message": "登录尝试过多，请稍后重试"},
        )

    encoded_hash = password_hash_for_role(settings, payload.role)
    if encoded_hash is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "auth_not_configured", "message": "登录密码尚未配置"},
        )

    password_valid = await asyncio.to_thread(
        verify_password, payload.password, encoded_hash.get_secret_value()
    )
    if not password_valid:
        rate_limiter.record_failure(limiter_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_credentials", "message": "角色或密码错误"},
        )

    rate_limiter.clear(limiter_key)
    session_token = generate_token()
    csrf_token = generate_token()
    expires_at = utc_now() + session_lifetime(settings, payload.role)
    database.add(
        Session(
            role=payload.role,
            token_hash=hash_token(session_token),
            csrf_token_hash=hash_token(csrf_token),
            expires_at=expires_at,
        )
    )
    await database.commit()

    max_age = int(session_lifetime(settings, payload.role).total_seconds())
    set_auth_cookies(response, settings, session_token, csrf_token, max_age)
    return SessionResponse(role=payload.role, expires_at=expires_at)


@router.get("/session", response_model=SessionResponse)
async def current_session(auth_session: CurrentSession) -> SessionResponse:
    return SessionResponse(role=auth_session.role, expires_at=auth_session.expires_at)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    database: DatabaseSession,
    auth_session: CsrfSession,
) -> None:
    require_same_origin(request)
    await database.delete(auth_session)
    await database.commit()
    settings: Settings = request.app.state.settings
    clear_auth_cookies(response, settings)
