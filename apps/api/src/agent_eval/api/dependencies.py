from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from agent_eval.auth.identity import legacy_subject_key
from agent_eval.auth.security import hash_token, raw_tokens_match, require_same_origin, tokens_match
from agent_eval.core.settings import Settings
from agent_eval.db.base import ensure_utc, utc_now
from agent_eval.db.enums import SessionRole
from agent_eval.db.models import AuthUser
from agent_eval.db.models import Session as AuthSession


async def get_database_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


DatabaseSession = Annotated[AsyncSession, Depends(get_database_session)]


async def get_current_session(request: Request, database: DatabaseSession) -> AuthSession:
    settings: Settings = request.app.state.settings
    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "not_authenticated", "message": "请先登录"},
        )

    auth_session = await database.scalar(
        select(AuthSession)
        .options(joinedload(AuthSession.user))
        .where(AuthSession.token_hash == hash_token(session_token))
    )
    if auth_session is None or ensure_utc(auth_session.expires_at) <= utc_now():
        if auth_session is not None:
            await database.delete(auth_session)
            await database.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "session_expired", "message": "登录已失效"},
        )

    if auth_session.user is None:
        legacy_user = await database.scalar(
            select(AuthUser).where(AuthUser.subject_key == legacy_subject_key(auth_session.role))
        )
        if legacy_user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "session_expired", "message": "登录已失效"},
            )
        # Rolling upgrades can briefly encounter sessions written before user_id existed.
        auth_session.user = legacy_user
    if auth_session.user.disabled_at is not None:
        await database.delete(auth_session)
        await database.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "user_disabled", "message": "用户已停用"},
        )
    return auth_session


CurrentSession = Annotated[AuthSession, Depends(get_current_session)]


async def require_operator(auth_session: CurrentSession) -> AuthSession:
    if auth_session.role != SessionRole.OPERATOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "operator_required", "message": "需要运营权限"},
        )
    return auth_session


OperatorSession = Annotated[AuthSession, Depends(require_operator)]


async def require_csrf(
    request: Request,
    auth_session: CurrentSession,
) -> AuthSession:
    require_same_origin(request)
    settings: Settings = request.app.state.settings
    cookie_token = request.cookies.get(settings.csrf_cookie_name)
    header_token = request.headers.get("x-csrf-token")
    if (
        cookie_token is None
        or header_token is None
        or not tokens_match(header_token, auth_session.csrf_token_hash)
        or not raw_tokens_match(cookie_token, header_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "csrf_failed", "message": "CSRF 校验失败"},
        )
    return auth_session


CsrfSession = Annotated[AuthSession, Depends(require_csrf)]


async def require_operator_csrf(auth_session: CsrfSession) -> AuthSession:
    if auth_session.role != SessionRole.OPERATOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "operator_required", "message": "需要运营权限"},
        )
    return auth_session


OperatorCsrfSession = Annotated[AuthSession, Depends(require_operator_csrf)]


async def require_workspace_session(auth_session: CurrentSession) -> AuthSession:
    user = auth_session.user
    if user is None or user.disabled_at is not None or not user.workspace_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "workspace_not_enabled", "message": "此用户未启用工作区"},
        )
    return auth_session


WorkspaceSession = Annotated[AuthSession, Depends(require_workspace_session)]


async def require_workspace_csrf(
    auth_session: Annotated[AuthSession, Depends(require_csrf)],
) -> AuthSession:
    return await require_workspace_session(auth_session)


WorkspaceCsrf = Annotated[AuthSession, Depends(require_workspace_csrf)]
