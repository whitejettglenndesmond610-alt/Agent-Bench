import asyncio
import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agent_eval.core.artifact_store import ArtifactStore
from agent_eval.core.settings import Settings
from agent_eval.db.base import utc_now
from agent_eval.db.enums import ResultValidityStatus, RunScope, RunStatus
from agent_eval.db.models import (
    ArtifactIndex,
    EvaluationRun,
    LeaderboardResult,
    LeaderboardSnapshot,
    RunAgent,
)
from agent_eval.leaderboards.validity import classify_result_validity
from agent_eval.runs.profiles import UNIFIED_V3_PROFILE_KEY, UNIFIED_V3_PROFILE_KEYS


@dataclass(frozen=True)
class BackupManifest:
    filename: str
    sha256: str
    size_bytes: int
    created_at: str
    alembic_version: str


@dataclass(frozen=True)
class RevalidationReport:
    run_count: int
    run_agent_count: int
    valid_count: int
    invalid_count: int
    updated_count: int
    deleted_result_count: int
    current_repaired_count: int


def create_backup(settings: Settings) -> BackupManifest:
    """Create a SQLite-only backup; artifact objects are deliberately not included."""
    source_path = sqlite_database_path(settings)
    if not source_path.is_file():
        raise RuntimeError("SQLite database does not exist")
    settings.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    identifier = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    final_path = settings.backup_dir / f"agent-eval-{identifier}.db"
    temporary_path = settings.backup_dir / f".{final_path.name}.tmp"
    try:
        with (
            sqlite3.connect(source_path) as source,
            sqlite3.connect(temporary_path) as destination,
        ):
            source.backup(destination)
            integrity = destination.execute("PRAGMA quick_check").fetchone()
            if integrity != ("ok",):
                raise RuntimeError("backup integrity check failed")
            version_row = destination.execute(
                "SELECT version_num FROM alembic_version LIMIT 1"
            ).fetchone()
            if version_row is None:
                raise RuntimeError("backup has no Alembic version")
            alembic_version = str(version_row[0])
        temporary_path.chmod(0o600)
        _fsync_file(temporary_path)
        os.replace(temporary_path, final_path)
        manifest = BackupManifest(
            filename=final_path.name,
            sha256=hashlib.sha256(final_path.read_bytes()).hexdigest(),
            size_bytes=final_path.stat().st_size,
            created_at=datetime.now(UTC).isoformat(),
            alembic_version=alembic_version,
        )
        _write_json_atomic(settings.backup_dir / "latest.json", asdict(manifest))
        _fsync_directory(settings.backup_dir)
        _remove_old_backups(settings.backup_dir, keep=final_path.name)
        return manifest
    finally:
        temporary_path.unlink(missing_ok=True)


def verify_latest_backup(settings: Settings) -> BackupManifest:
    manifest = _load_manifest(settings.backup_dir / "latest.json")
    backup_path = _safe_backup_path(settings.backup_dir, manifest.filename)
    if not backup_path.is_file():
        raise RuntimeError("latest backup file is missing")
    if backup_path.stat().st_size != manifest.size_bytes:
        raise RuntimeError("latest backup size mismatch")
    if hashlib.sha256(backup_path.read_bytes()).hexdigest() != manifest.sha256:
        raise RuntimeError("latest backup hash mismatch")
    with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("latest backup integrity check failed")
        version_row = connection.execute(
            "SELECT version_num FROM alembic_version LIMIT 1"
        ).fetchone()
    if version_row is None or str(version_row[0]) != manifest.alembic_version:
        raise RuntimeError("latest backup Alembic version mismatch")
    return manifest


def restore_latest_backup(settings: Settings, *, confirm_stopped: bool) -> BackupManifest:
    if not confirm_stopped:
        raise RuntimeError("restore requires --confirm-stopped")
    manifest = verify_latest_backup(settings)
    backup_path = _safe_backup_path(settings.backup_dir, manifest.filename)
    database_path = sqlite_database_path(settings)
    temporary_path = database_path.with_name(f".{database_path.name}.restore-{uuid4().hex}.tmp")
    try:
        with (
            sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as source,
            sqlite3.connect(temporary_path) as destination,
        ):
            source.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("restored database integrity check failed")
        temporary_path.chmod(0o600)
        _fsync_file(temporary_path)
        os.replace(temporary_path, database_path)
        for suffix in ("-wal", "-shm"):
            database_path.with_name(database_path.name + suffix).unlink(missing_ok=True)
        _fsync_directory(database_path.parent)
        return manifest
    finally:
        temporary_path.unlink(missing_ok=True)


async def cleanup_expired_artifacts(
    session: AsyncSession, artifact_store: ArtifactStore
) -> tuple[int, int]:
    """Delete expired objects and their rows, committing each successful object independently.

    A process failure between object deletion and the row commit can leave an index row for a
    missing object. Missing deletes are intentionally successful on retry.
    """
    artifacts = [
        (artifact.id, artifact.relative_path)
        for artifact in await session.scalars(
            select(ArtifactIndex)
            .where(ArtifactIndex.expires_at <= utc_now())
            .order_by(ArtifactIndex.id)
        )
    ]
    removed_objects = 0
    removed_rows = 0
    for artifact_id, relative_path in artifacts:
        existed = await asyncio.to_thread(artifact_store.delete, relative_path)
        removed_objects += existed
        artifact = await session.get(ArtifactIndex, artifact_id)
        if artifact is None:
            continue
        await session.delete(artifact)
        await session.commit()
        removed_rows += 1
    return removed_rows, removed_objects


async def archive_current_leaderboard(session: AsyncSession) -> int:
    snapshot_ids = list(
        await session.scalars(
            select(LeaderboardSnapshot.id).where(LeaderboardSnapshot.is_current.is_(True))
        )
    )
    if snapshot_ids:
        await session.execute(
            update(LeaderboardSnapshot)
            .where(LeaderboardSnapshot.id.in_(snapshot_ids))
            .values(is_current=False)
        )
    await session.flush()
    return len(snapshot_ids)


async def revalidate_ranking_results(session: AsyncSession) -> RevalidationReport:
    run_agents = list(
        await session.scalars(
            select(RunAgent)
            .options(selectinload(RunAgent.execution_items))
            .join(EvaluationRun, EvaluationRun.id == RunAgent.run_id)
            .where(
                EvaluationRun.status == RunStatus.PUBLISHED,
                EvaluationRun.ranking_eligible.is_(True),
                EvaluationRun.profile_key.in_(UNIFIED_V3_PROFILE_KEYS),
            )
            .order_by(RunAgent.run_id, RunAgent.id)
        )
    )
    updated_count = 0
    invalid_ids: list[UUID] = []
    valid_count = 0
    for run_agent in run_agents:
        validity = classify_result_validity(item.error_type for item in run_agent.execution_items)
        changed = (
            run_agent.validity_status != validity.status
            or run_agent.validity_reason != validity.reason
            or run_agent.configuration_failure_count != validity.configuration_failure_count
            or run_agent.configuration_failure_rate != validity.configuration_failure_rate
        )
        run_agent.validity_status = validity.status
        run_agent.validity_reason = validity.reason
        run_agent.configuration_failure_count = validity.configuration_failure_count
        run_agent.configuration_failure_rate = validity.configuration_failure_rate
        updated_count += changed
        if validity.status == ResultValidityStatus.INVALID:
            invalid_ids.append(run_agent.id)
        else:
            valid_count += 1
    await session.flush()

    deleted_result_count = 0
    if invalid_ids:
        deleted_result_count = await session.scalar(
            select(func.count())
            .select_from(LeaderboardResult)
            .where(LeaderboardResult.run_agent_id.in_(invalid_ids))
        ) or 0
        await session.execute(
            delete(LeaderboardResult).where(LeaderboardResult.run_agent_id.in_(invalid_ids))
        )

    current_repaired_count = 0
    current = await session.scalar(
        select(LeaderboardSnapshot)
        .join(EvaluationRun, EvaluationRun.id == LeaderboardSnapshot.run_id)
        .where(
            LeaderboardSnapshot.is_current.is_(True),
            EvaluationRun.profile_key == UNIFIED_V3_PROFILE_KEY,
            EvaluationRun.run_scope == RunScope.COMPREHENSIVE,
        )
    )
    if current is not None:
        valid_result_count = await session.scalar(
            select(func.count())
            .select_from(LeaderboardResult)
            .join(RunAgent, RunAgent.id == LeaderboardResult.run_agent_id)
            .where(
                LeaderboardResult.snapshot_id == current.id,
                RunAgent.validity_status == ResultValidityStatus.VALID,
            )
        )
        if not valid_result_count:
            current.is_current = False
            await session.flush()
            replacement = await session.scalar(
                select(LeaderboardSnapshot)
                .join(EvaluationRun, EvaluationRun.id == LeaderboardSnapshot.run_id)
                .join(
                    LeaderboardResult,
                    LeaderboardResult.snapshot_id == LeaderboardSnapshot.id,
                )
                .join(RunAgent, RunAgent.id == LeaderboardResult.run_agent_id)
                .where(
                    EvaluationRun.profile_key == UNIFIED_V3_PROFILE_KEY,
                    EvaluationRun.run_scope == RunScope.COMPREHENSIVE,
                    EvaluationRun.ranking_eligible.is_(True),
                    RunAgent.validity_status == ResultValidityStatus.VALID,
                )
                .order_by(
                    LeaderboardSnapshot.published_at.desc(),
                    LeaderboardSnapshot.id.desc(),
                )
                .limit(1)
            )
            if replacement is not None:
                replacement.is_current = True
            current_repaired_count = 1
    await session.flush()
    return RevalidationReport(
        run_count=len({run_agent.run_id for run_agent in run_agents}),
        run_agent_count=len(run_agents),
        valid_count=valid_count,
        invalid_count=len(invalid_ids),
        updated_count=updated_count,
        deleted_result_count=deleted_result_count,
        current_repaired_count=current_repaired_count,
    )


def sqlite_database_path(settings: Settings) -> Path:
    url = make_url(settings.database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        raise RuntimeError("maintenance operation requires a file-backed SQLite database")
    return Path(url.database).resolve()


def _load_manifest(path: Path) -> BackupManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return BackupManifest(**payload)
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise RuntimeError("latest backup manifest is invalid") from error


def _safe_backup_path(backup_dir: Path, filename: str) -> Path:
    if Path(filename).name != filename:
        raise RuntimeError("backup filename is unsafe")
    root = backup_dir.resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root):
        raise RuntimeError("backup path escapes storage root")
    return path


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        _fsync_file(temporary_path)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _remove_old_backups(backup_dir: Path, *, keep: str) -> None:
    for path in backup_dir.glob("agent-eval-*.db"):
        if path.name != keep:
            path.unlink()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as file_handle:
        os.fsync(file_handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
