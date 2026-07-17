import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_eval.core.settings import Settings
from agent_eval.db.engine import create_engine
from agent_eval.db.models import ResourceLease
from agent_eval.db.resource_leases import ResourceLeaseManager
from agent_eval.worker.citations import citation_domain
from agent_eval.worker.limits import ResourceLimiter, concurrency_slot


def alembic_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


async def migrated_sqlite(tmp_path: Path) -> tuple[AsyncEngine, ResourceLeaseManager]:
    database_path = tmp_path / "resource-leases.db"
    await asyncio.to_thread(command.upgrade, alembic_config(database_path), "head")
    engine = create_engine(Settings(database_url=f"sqlite+aiosqlite:///{database_path}"))
    return engine, ResourceLeaseManager(engine, "test-owner", lease_seconds=0.2)


async def test_sqlite_lease_acquire_release_and_idempotency(tmp_path: Path) -> None:
    engine, manager = await migrated_sqlite(tmp_path)
    try:
        first = await manager.acquire("request", "judge", 1)
        assert first is not None
        assert await manager.acquire("request", "judge", 1) is None

        await manager.release(first)
        await manager.release(first)
        second = await manager.acquire("request", "judge", 1)
        assert second is not None
        assert second.holder_token != first.holder_token
    finally:
        await engine.dispose()


async def test_sqlite_expired_lease_is_reclaimed_and_old_token_is_fenced(
    tmp_path: Path,
) -> None:
    engine, manager = await migrated_sqlite(tmp_path)
    try:
        stale = await manager.acquire("request", "agent", 1)
        assert stale is not None
        await asyncio.sleep(1.05)

        replacement = await manager.acquire("request", "agent", 1)
        assert replacement is not None
        assert replacement.holder_token != stale.holder_token
        await manager.release(stale)

        async with engine.connect() as connection:
            tokens = list(
                (await connection.execute(select(ResourceLease.holder_token))).scalars()
            )
        assert tokens == [replacement.holder_token]
    finally:
        await engine.dispose()


async def test_limiter_cancellation_releases_database_lease(tmp_path: Path) -> None:
    engine, manager = await migrated_sqlite(tmp_path)
    limiter = ResourceLimiter(
        1,
        resource_type="request",
        default_key="citation-fetch",
        manager=manager,
        poll_seconds=0.01,
    )
    entered = asyncio.Event()

    async def hold() -> None:
        async with concurrency_slot(limiter):
            entered.set()
            await asyncio.Event().wait()

    try:
        task = asyncio.create_task(hold())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        async with engine.connect() as connection:
            count = await connection.scalar(select(func.count()).select_from(ResourceLease))
        assert count == 0
    finally:
        await engine.dispose()


async def test_keyed_domain_limiter_is_shared_across_runs() -> None:
    limiter = ResourceLimiter(1, resource_type="citation-domain")
    active = 0
    maximum = 0

    async def fetch_for_run() -> None:
        nonlocal active, maximum
        async with concurrency_slot(limiter, "example.com"):
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(fetch_for_run() for _ in range(4)))
    assert maximum == 1


def test_citation_domain_key_uses_canonical_url_host() -> None:
    assert citation_domain("HTTPS://Example.COM./source#fragment") == "example.com"
    assert citation_domain("https://例子.测试/source") == "xn--fsqu00a.xn--0zwm56d"
