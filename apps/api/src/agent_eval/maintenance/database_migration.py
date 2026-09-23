import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import DateTime, Table, delete, event, func, insert, select, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from agent_eval.auth.identity import LEGACY_OPERATOR_USER_ID, LEGACY_READ_ONLY_USER_ID
from agent_eval.db.base import Base, ensure_utc
from agent_eval.db.engine import configure_sqlite_connection
from agent_eval.db.enums import WorkspaceAttemptStatus
from agent_eval.db.models import (
    AuthUser,
    EvaluationRun,
    LeaderboardSnapshot,
    ResourceLease,
    WorkerLease,
    WorkspaceAttempt,
)

ALEMBIC_HEAD = "f6e1d2c3b4a5"
SOURCE_DRIVER = "sqlite+aiosqlite"
TARGET_DRIVER = "postgresql+asyncpg"
EXCLUDED_TABLES = frozenset({"resource_leases", "worker_leases"})


class DatabaseMigrationError(RuntimeError):
    """A migration refusal or failure whose message is safe to show to operators."""


@dataclass(frozen=True)
class TableFingerprint:
    table: str
    row_count: int
    sha256: str


@dataclass(frozen=True)
class DatabaseMigrationReport:
    dry_run: bool
    tables: tuple[TableFingerprint, ...]

    @property
    def total_rows(self) -> int:
        return sum(table.row_count for table in self.tables)


def validate_migration_urls(source_url: str, target_url: str) -> None:
    try:
        source = make_url(source_url)
        target = make_url(target_url)
    except (TypeError, ValueError) as error:
        raise DatabaseMigrationError("source or target database URL is invalid") from error
    if source.drivername != SOURCE_DRIVER:
        raise DatabaseMigrationError(f"source database must use the {SOURCE_DRIVER} driver")
    if target.drivername != TARGET_DRIVER:
        raise DatabaseMigrationError(f"target database must use the {TARGET_DRIVER} driver")


def migration_tables() -> tuple[Table, ...]:
    tables = tuple(
        table for table in Base.metadata.sorted_tables if table.name not in EXCLUDED_TABLES
    )
    positions = {table: position for position, table in enumerate(tables)}
    expected = set(Base.metadata.tables.values()) - {
        ResourceLease.__table__,
        WorkerLease.__table__,
    }
    if set(tables) != expected:
        raise DatabaseMigrationError("model metadata contains an unsupported table dependency")
    for table in tables:
        for foreign_key in table.foreign_keys:
            parent = foreign_key.column.table
            if parent is table or parent not in positions or positions[parent] >= positions[table]:
                raise DatabaseMigrationError(
                    "model metadata contains a self-referential or cyclic table dependency"
                )
    return tables


async def migrate_database(
    *,
    source_url: str,
    target_url: str,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> DatabaseMigrationReport:
    """Copy a stopped SQLite deployment into an empty, migrated PostgreSQL database."""
    validate_migration_urls(source_url, target_url)
    if batch_size < 1:
        raise DatabaseMigrationError("batch size must be at least 1")

    tables = migration_tables()
    source_engine = _create_source_engine(source_url)
    target_engine = create_async_engine(target_url, pool_pre_ping=True)
    target_committed = False
    try:
        async with (
            source_engine.connect() as source,
            target_engine.connect() as target,
            source.begin(),
        ):
            target_transaction = await target.begin()
            try:
                await _validate_alembic_head(source, "source")
                await _validate_alembic_head(target, "target")
                await _validate_target_schema(target)
                await _lock_target_tables(target)
                await _validate_source(source)
                await _validate_empty_target(target)
                source_inventory = await fingerprint_database(
                    source, tables=tables, batch_size=batch_size
                )
                await _validate_invariants(source, "source")

                if dry_run:
                    await target_transaction.rollback()
                    return DatabaseMigrationReport(dry_run=True, tables=source_inventory)

                await target.execute(delete(AuthUser))
                for table in tables:
                    await _copy_table(
                        source,
                        target,
                        table=table,
                        batch_size=batch_size,
                    )

                target_inventory = await fingerprint_database(
                    target, tables=tables, batch_size=batch_size
                )
                _compare_inventories(source_inventory, target_inventory)
                await _validate_invariants(target, "target")
                await target_transaction.commit()
                target_committed = True

                committed_inventory = await fingerprint_database(
                    target, tables=tables, batch_size=batch_size
                )
                _compare_inventories(source_inventory, committed_inventory)
                await _validate_invariants(target, "target after commit")
                return DatabaseMigrationReport(dry_run=False, tables=committed_inventory)
            except Exception:
                if target_transaction.is_active:
                    await target_transaction.rollback()
                raise
    except DatabaseMigrationError:
        if target_committed:
            await _clear_failed_copy(target_engine, tables)
        raise
    except Exception as error:
        if target_committed:
            await _clear_failed_copy(target_engine, tables)
        raise DatabaseMigrationError(
            "database migration failed; the target copy was rolled back"
        ) from error
    finally:
        await source_engine.dispose()
        await target_engine.dispose()


async def validate_sqlite_source(connection: AsyncConnection) -> None:
    """Validate source-only safety preconditions, exposed for focused SQLite tests."""
    if connection.dialect.name != "sqlite":
        raise DatabaseMigrationError("source validation requires SQLite")
    await _validate_alembic_head(connection, "source")
    await _validate_source(connection)
    await _validate_invariants(connection, "source")


async def fingerprint_database(
    connection: AsyncConnection,
    *,
    tables: Sequence[Table] | None = None,
    batch_size: int = 1000,
) -> tuple[TableFingerprint, ...]:
    selected_tables = tuple(tables) if tables is not None else migration_tables()
    return tuple(
        [
            await _fingerprint_table(connection, table=table, batch_size=batch_size)
            for table in selected_tables
        ]
    )


def _create_source_engine(source_url: str) -> AsyncEngine:
    engine = create_async_engine(source_url, pool_pre_ping=True)
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    return engine


async def _validate_alembic_head(connection: AsyncConnection, label: str) -> None:
    try:
        versions = list(
            (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars()
        )
    except SQLAlchemyError as error:
        raise DatabaseMigrationError(f"{label} database has no readable Alembic version") from error
    if versions != [ALEMBIC_HEAD]:
        raise DatabaseMigrationError(f"{label} database is not at required Alembic head")


async def _validate_source(connection: AsyncConnection) -> None:
    active_runs = await connection.scalar(
        select(func.count())
        .select_from(EvaluationRun)
        .where(EvaluationRun.active_slot.is_not(None))
    )
    if active_runs:
        raise DatabaseMigrationError("source database has active evaluation runs")
    active_workspace_attempts = await connection.scalar(
        select(func.count())
        .select_from(WorkspaceAttempt)
        .where(
            WorkspaceAttempt.status.in_(
                [WorkspaceAttemptStatus.QUEUED, WorkspaceAttemptStatus.RUNNING]
            )
        )
    )
    if active_workspace_attempts:
        raise DatabaseMigrationError("source database has active workspace attempts")
    worker_leases = await connection.scalar(select(func.count()).select_from(WorkerLease))
    if worker_leases:
        raise DatabaseMigrationError(
            "source database has worker leases; stop all workers and remove expired leases"
        )
    active_resource_leases = await connection.scalar(
        select(func.count())
        .select_from(ResourceLease)
        .where(ResourceLease.lease_expires_at > func.current_timestamp())
    )
    if active_resource_leases:
        raise DatabaseMigrationError(
            "source database has active resource leases; stop all request holders"
        )
    foreign_key_failures = list(
        (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all()
    )
    if foreign_key_failures:
        raise DatabaseMigrationError("source database contains foreign key violations")


async def _validate_empty_target(connection: AsyncConnection) -> None:
    for table in Base.metadata.sorted_tables:
        count = await connection.scalar(select(func.count()).select_from(table))
        if table is AuthUser.__table__:
            legacy_rows = list(
                (
                    await connection.execute(
                        select(AuthUser.id, AuthUser.subject_key, AuthUser.auth_source).order_by(
                            AuthUser.id
                        )
                    )
                ).all()
            )
            expected = {
                (
                    LEGACY_READ_ONLY_USER_ID,
                    "legacy_shared:read_only",
                    "legacy_shared",
                ),
                (
                    LEGACY_OPERATOR_USER_ID,
                    "legacy_shared:operator",
                    "legacy_shared",
                ),
            }
            normalized = {
                (UUID(str(user_id)), subject_key, getattr(auth_source, "value", auth_source))
                for user_id, subject_key, auth_source in legacy_rows
            }
            if normalized == expected:
                continue
        if count:
            raise DatabaseMigrationError(
                f"target database is not empty: table {table.name} contains data"
            )


async def _validate_invariants(connection: AsyncConnection, label: str) -> None:
    active_runs = await connection.scalar(
        select(func.count())
        .select_from(EvaluationRun)
        .where(EvaluationRun.active_slot.is_not(None))
    )
    if active_runs:
        raise DatabaseMigrationError(f"{label} database has active evaluation runs")
    active_workspace_attempts = await connection.scalar(
        select(func.count())
        .select_from(WorkspaceAttempt)
        .where(
            WorkspaceAttempt.status.in_(
                [WorkspaceAttemptStatus.QUEUED, WorkspaceAttemptStatus.RUNNING]
            )
        )
    )
    if active_workspace_attempts:
        raise DatabaseMigrationError(f"{label} database has active workspace attempts")
    current_snapshots = await connection.scalar(
        select(func.count())
        .select_from(LeaderboardSnapshot)
        .where(LeaderboardSnapshot.is_current.is_(True))
    )
    if current_snapshots is not None and current_snapshots > 1:
        raise DatabaseMigrationError(
            f"{label} database has more than one current leaderboard snapshot"
        )
    worker_leases = await connection.scalar(select(func.count()).select_from(WorkerLease))
    if worker_leases:
        raise DatabaseMigrationError(f"{label} database has worker leases")
    active_resource_leases = await connection.scalar(
        select(func.count())
        .select_from(ResourceLease)
        .where(ResourceLease.lease_expires_at > func.current_timestamp())
    )
    if active_resource_leases:
        raise DatabaseMigrationError(f"{label} database has active resource leases")


async def _validate_target_schema(connection: AsyncConnection) -> None:
    difference_count = await connection.run_sync(_target_schema_difference_count)
    if difference_count:
        raise DatabaseMigrationError(
            f"target schema does not match model metadata ({difference_count} differences)"
        )


def _target_schema_difference_count(connection: Connection) -> int:
    def include_object(
        object_: Any,
        name: str | None,
        type_: str,
        reflected: bool,
        compare_to: Any,
    ) -> bool:
        del object_, reflected, compare_to
        return not (type_ == "table" and name == "alembic_version")

    context = MigrationContext.configure(
        connection=connection,
        opts={"compare_type": True, "include_object": include_object},
    )
    return len(compare_metadata(context, Base.metadata))


async def _lock_target_tables(connection: AsyncConnection) -> None:
    preparer = connection.dialect.identifier_preparer
    names = ", ".join(preparer.quote(table.name) for table in Base.metadata.sorted_tables)
    await connection.exec_driver_sql(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE")


async def _copy_table(
    source: AsyncConnection,
    target: AsyncConnection,
    *,
    table: Table,
    batch_size: int,
) -> None:
    statement = select(table).order_by(*table.primary_key.columns)
    result = await source.stream(statement)
    try:
        async for rows in result.partitions(batch_size):
            values = [_prepare_row(table, row._mapping) for row in rows]
            if values:
                await target.execute(table.insert(), values)
    finally:
        await result.close()


def _prepare_row(table: Table, row: Mapping[Any, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for column in table.columns:
        value = row[column.name]
        if (
            value is not None
            and isinstance(column.type, DateTime)
            and column.type.timezone
            and isinstance(value, datetime)
        ):
            value = ensure_utc(value)
        values[column.name] = value
    return values


async def _fingerprint_table(
    connection: AsyncConnection,
    *,
    table: Table,
    batch_size: int,
) -> TableFingerprint:
    digest = hashlib.sha256()
    digest.update(
        json.dumps([column.name for column in table.columns], separators=(",", ":")).encode()
    )
    digest.update(b"\n")
    row_count = 0
    result = await connection.stream(select(table).order_by(*table.primary_key.columns))
    try:
        async for rows in result.partitions(batch_size):
            for row in rows:
                canonical = [
                    _canonical_value(row._mapping[column.name]) for column in table.columns
                ]
                digest.update(
                    json.dumps(canonical, ensure_ascii=True, separators=(",", ":")).encode()
                )
                digest.update(b"\n")
                row_count += 1
    finally:
        await result.close()
    return TableFingerprint(table=table.name, row_count=row_count, sha256=digest.hexdigest())


def _canonical_value(value: Any) -> object:
    if value is None:
        return ["null"]
    if isinstance(value, Enum):
        return ["enum", value.value]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, Decimal):
        normalized = format(value, "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        if Decimal(normalized) == 0:
            normalized = "0"
        return ["decimal", normalized]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, UUID):
        return ["uuid", str(value)]
    if isinstance(value, datetime):
        return ["datetime", ensure_utc(value).isoformat(timespec="microseconds")]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, bytes):
        return ["bytes", base64.b64encode(value).decode("ascii")]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, list):
        return ["list", [_canonical_value(item) for item in value]]
    if isinstance(value, dict):
        return [
            "dict",
            [[str(key), _canonical_value(value[key])] for key in sorted(value, key=str)],
        ]
    raise DatabaseMigrationError(
        f"cannot fingerprint unsupported database value type {type(value).__name__}"
    )


def _compare_inventories(
    source: Sequence[TableFingerprint], target: Sequence[TableFingerprint]
) -> None:
    if source != target:
        raise DatabaseMigrationError("source and target counts or fingerprints do not match")


async def _clear_failed_copy(target_engine: AsyncEngine, tables: Sequence[Table]) -> None:
    try:
        async with target_engine.begin() as connection:
            await _lock_target_tables(connection)
            for table in reversed(tables):
                await connection.execute(table.delete())
            now = await connection.scalar(select(func.current_timestamp()))
            if now is None:
                raise DatabaseMigrationError("target database time is unavailable")
            await connection.execute(
                insert(AuthUser),
                [
                    {
                        "id": LEGACY_READ_ONLY_USER_ID,
                        "subject_key": "legacy_shared:read_only",
                        "username": None,
                        "display_name": "Shared read-only",
                        "role": "read_only",
                        "auth_source": "legacy_shared",
                        "password_hash": None,
                        "is_shared": True,
                        "workspace_enabled": False,
                        "disabled_at": None,
                        "created_at": now,
                        "updated_at": now,
                    },
                    {
                        "id": LEGACY_OPERATOR_USER_ID,
                        "subject_key": "legacy_shared:operator",
                        "username": None,
                        "display_name": "Shared operator",
                        "role": "operator",
                        "auth_source": "legacy_shared",
                        "password_hash": None,
                        "is_shared": True,
                        "workspace_enabled": True,
                        "disabled_at": None,
                        "created_at": now,
                        "updated_at": now,
                    },
                ],
            )
    except SQLAlchemyError as error:
        raise DatabaseMigrationError(
            "post-commit verification failed and automatic target cleanup failed; "
            "keep the target offline"
        ) from error
