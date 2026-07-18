import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class MutationLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


@asynccontextmanager
async def sqlite_mutation_guard(
    request: Request, database: AsyncSession, key: str
) -> AsyncIterator[None]:
    if database.get_bind().dialect.name != "sqlite":
        yield
        return
    locks: dict[str, MutationLock] = getattr(
        request.app.state, "sqlite_mutation_locks", {}
    )
    request.app.state.sqlite_mutation_locks = locks
    entry = locks.setdefault(key, MutationLock())
    entry.users += 1
    try:
        async with entry.lock:
            # Authentication opened a read transaction before this lock was acquired.
            await database.rollback()
            yield
    finally:
        entry.users -= 1
        if entry.users == 0 and locks.get(key) is entry:
            locks.pop(key)
