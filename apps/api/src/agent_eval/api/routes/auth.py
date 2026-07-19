import asyncio
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import SecretStr
from sqlalchemy import select

from agent_eval.api.dependencies import CsrfSession, CurrentSession, DatabaseSession
from agent_eval.api.schemas.auth import LoginRequest, SessionResponse, UserLoginRequest
from agent_eval.auth.identity import (
    legacy_user_id,
    normalize_username,
    shared_browser_subject_key,
)
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
from agent_eval.db.enums import AuthSource, SessionRole
from agent_eval.db.models import AuthUser, Session

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$93aaDfctggwyeKiEjQKX4A$"
    "ejTnwi41+V5Q/ABxvAaqXC5F9lmum9/DoYZq4uEiKR4"
)


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


def set_workspace_identity_cookie(
    response: Response, settings: Settings, identity_token: str
) -> None:
    response.set_cookie(
        settings.workspace_identity_cookie_name,
        identity_token,
        httponly=True,
        max_age=settings.workspace_identity_days * 24 * 60 * 60,
        path="/api/v1/auth/login",
        secure=settings.secure_cookies,
        samesite="strict",
    )


def session_response(auth_session: Session) -> SessionResponse:
    user = auth_session.user
    if user is None:
        raise RuntimeError("authenticated session has no resolved user")
    return SessionResponse(
        role=auth_session.role,
        expires_at=auth_session.expires_at,
        user_id=user.id,
        display_name=user.display_name,
        is_shared=user.is_shared,
        workspace_enabled=user.workspace_enabled,
    )


async def issue_session(
    database: DatabaseSession,
    response: Response,
    settings: Settings,
    user: AuthUser,
) -> SessionResponse:
    session_token = generate_token()
    csrf_token = generate_token()
    lifetime = session_lifetime(settings, user.role)
    expires_at = utc_now() + lifetime
    auth_session = Session(
        user_id=user.id,
        role=user.role,
        token_hash=hash_token(session_token),
        csrf_token_hash=hash_token(csrf_token),
        expires_at=expires_at,
        user=user,
    )
    database.add(auth_session)
    await database.commit()
    set_auth_cookies(
        response,
        settings,
        session_token,
        csrf_token,
        int(lifetime.total_seconds()),
    )
    return session_response(auth_session)


async def resolve_shared_browser_user(
    request: Request,
    database: DatabaseSession,
    settings: Settings,
) -> tuple[AuthUser, str]:
    identity_token = request.cookies.get(settings.workspace_identity_cookie_name)
    if identity_token is not None:
        try:
            subject_key = shared_browser_subject_key(identity_token)
        except ValueError:
            identity_token = None
        else:
            user = await database.scalar(
                select(AuthUser).where(AuthUser.subject_key == subject_key)
            )
            if user is not None and user.disabled_at is None:
                user.updated_at = utc_now()
                return user, identity_token

    identity_token = generate_token()
    user = AuthUser(
        subject_key=shared_browser_subject_key(identity_token),
        username=None,
        display_name="普通用户",
        role=SessionRole.READ_ONLY,
        auth_source=AuthSource.SHARED_BROWSER,
        password_hash=None,
        is_shared=True,
        workspace_enabled=True,
    )
    database.add(user)
    await database.flush()
    return user, identity_token


def enforce_login_rate_limit(rate_limiter: LoginRateLimiter, limiter_key: str) -> None:
    if retry_after := rate_limiter.retry_after(limiter_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
            detail={"code": "login_rate_limited", "message": "登录尝试过多，请稍后重试"},
        )


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

    enforce_login_rate_limit(rate_limiter, limiter_key)

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
    if payload.role == SessionRole.READ_ONLY:
        user, identity_token = await resolve_shared_browser_user(request, database, settings)
        result = await issue_session(database, response, settings, user)
        set_workspace_identity_cookie(response, settings, identity_token)
        return result

    operator_user = await database.get(AuthUser, legacy_user_id(payload.role))
    if operator_user is None or operator_user.disabled_at is not None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "auth_not_configured", "message": "共享登录用户尚未配置"},
        )
    return await issue_session(database, response, settings, operator_user)


@router.post("/user-login", response_model=SessionResponse)
async def user_login(
    payload: UserLoginRequest,
    request: Request,
    response: Response,
    database: DatabaseSession,
) -> SessionResponse:
    require_same_origin(request)
    settings: Settings = request.app.state.settings
    rate_limiter: LoginRateLimiter = request.app.state.login_rate_limiter
    limiter_key = client_ip(request)
    enforce_login_rate_limit(rate_limiter, limiter_key)
    try:
        username = normalize_username(payload.username)
    except ValueError:
        username = ""
    user = await database.scalar(select(AuthUser).where(AuthUser.username == username))
    fallback_hash = settings.operator_password_hash or settings.read_only_password_hash
    encoded_hash = (
        user.password_hash
        if user is not None and user.password_hash is not None
        else (
            fallback_hash.get_secret_value()
            if fallback_hash is not None
            else DUMMY_PASSWORD_HASH
        )
    )
    password_valid = await asyncio.to_thread(verify_password, payload.password, encoded_hash)
    if not password_valid or user is None or user.disabled_at is not None:
        rate_limiter.record_failure(limiter_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_credentials", "message": "用户名或密码错误"},
        )
    rate_limiter.clear(limiter_key)
    return await issue_session(database, response, settings, user)


@router.get("/session", response_model=SessionResponse)
async def current_session(auth_session: CurrentSession) -> SessionResponse:
    return session_response(auth_session)


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
