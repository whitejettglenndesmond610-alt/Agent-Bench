import os
import secrets
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_eval.auth.identity import normalize_username
from agent_eval.auth.security import hash_password
from agent_eval.db.enums import AuthSource, SessionRole
from agent_eval.db.models import AuthUser, Session


class WorkspaceUserError(RuntimeError):
    pass


async def create_workspace_user(
    session: AsyncSession,
    *,
    username: str,
    display_name: str,
    role: SessionRole,
    password: str,
) -> AuthUser:
    normalized = normalize_username(username)
    name = display_name.strip()
    if not name or len(name) > 160:
        raise WorkspaceUserError("display name must contain 1 to 160 characters")
    if not password:
        raise WorkspaceUserError("password must not be empty")
    user = AuthUser(
        subject_key=f"local:{normalized}",
        username=normalized,
        display_name=name,
        role=role,
        auth_source=AuthSource.LOCAL,
        password_hash=hash_password(password),
        is_shared=False,
        workspace_enabled=True,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as error:
        raise WorkspaceUserError("workspace username already exists") from error
    return user


async def reset_workspace_user_password(
    session: AsyncSession, *, username: str, password: str
) -> AuthUser:
    normalized = normalize_username(username)
    if not password:
        raise WorkspaceUserError("password must not be empty")
    user = await session.scalar(select(AuthUser).where(AuthUser.username == normalized))
    if user is None or user.auth_source != AuthSource.LOCAL:
        raise WorkspaceUserError("workspace user does not exist")
    user.password_hash = hash_password(password)
    await session.execute(delete(Session).where(Session.user_id == user.id))
    await session.flush()
    return user


def generate_workspace_password() -> str:
    return secrets.token_urlsafe(24)


def write_credential(path: Path, *, username: str, password: str) -> None:
    parent = path.parent
    if not parent.is_dir():
        raise WorkspaceUserError("credential output parent directory does not exist")
    content = f"username={username}\npassword={password}\n".encode()
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except OSError as error:
        raise WorkspaceUserError("credential output path must not already exist") from error
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
