from sqlalchemy import event, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry

from agent_eval.core.settings import Settings


def configure_sqlite_connection(
    dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry
) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 10000")
        cursor.execute("PRAGMA synchronous = FULL")
    finally:
        cursor.close()


def create_engine(settings: Settings) -> AsyncEngine:
    is_sqlite = settings.database_url.startswith("sqlite+aiosqlite:")
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if not is_sqlite:
        kwargs.update(
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout=settings.database_pool_timeout_seconds,
        )
    engine = create_async_engine(settings.database_url, **kwargs)
    if is_sqlite:
        event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    return engine


async def verify_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
        await connection.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
