import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_eval.core.artifact_store import ArtifactStore, LocalArtifactStore, S3ArtifactStore
from agent_eval.core.settings import Settings
from agent_eval.db.engine import create_engine, verify_database
from agent_eval.db.models import ArtifactIndex
from agent_eval.db.session import create_session_factory


class ArtifactSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactSyncReport:
    total: int
    copied: int
    existing: int
    pending: int


@dataclass(frozen=True)
class _ArtifactRow:
    relative_path: str
    sha256: str
    size_bytes: int


async def sync_artifacts(
    session: AsyncSession,
    source: ArtifactStore,
    target: ArtifactStore,
    *,
    dry_run: bool,
    concurrency: int,
) -> ArtifactSyncReport:
    if concurrency < 1 or concurrency > 64:
        raise ValueError("artifact sync concurrency must be between 1 and 64")
    result = await session.execute(
        select(
            ArtifactIndex.relative_path,
            ArtifactIndex.sha256,
            ArtifactIndex.size_bytes,
        ).order_by(ArtifactIndex.id)
    )
    rows = [_ArtifactRow(path, sha256, size) for path, sha256, size in result.tuples()]
    copied = 0
    existing = 0
    for offset in range(0, len(rows), concurrency):
        outcomes = await asyncio.gather(
            *(
                _sync_one(source, target, row, dry_run=dry_run)
                for row in rows[offset : offset + concurrency]
            ),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise ArtifactSyncError("artifact sync failed integrity verification") from outcome
            copied += outcome == "copied"
            existing += outcome == "existing"
    return ArtifactSyncReport(
        total=len(rows),
        copied=copied,
        existing=existing,
        pending=len(rows) if dry_run else 0,
    )


async def _sync_one(
    source: ArtifactStore,
    target: ArtifactStore,
    row: _ArtifactRow,
    *,
    dry_run: bool,
) -> Literal["copied", "existing", "pending"]:
    content = await asyncio.to_thread(
        source.read,
        row.relative_path,
        expected_sha256=row.sha256,
        expected_size=row.size_bytes,
    )
    if dry_run:
        return "pending"
    try:
        await asyncio.to_thread(target.put, row.relative_path, content)
        outcome: Literal["copied", "existing"] = "copied"
    except FileExistsError:
        outcome = "existing"
    await asyncio.to_thread(
        target.read,
        row.relative_path,
        expected_sha256=row.sha256,
        expected_size=row.size_bytes,
    )
    return outcome


async def sync_local_artifacts_to_s3(
    *,
    settings: Settings,
    source_local_dir: Path,
    target_bucket: str,
    target_prefix: str,
    target_endpoint_url: str | None,
    target_region: str | None,
    target_force_path_style: bool,
    dry_run: bool,
    concurrency: int,
) -> ArtifactSyncReport:
    source = LocalArtifactStore(source_local_dir)
    target = S3ArtifactStore(
        bucket=target_bucket,
        prefix=target_prefix,
        endpoint_url=target_endpoint_url,
        region=target_region,
        force_path_style=target_force_path_style,
    )
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        async with session_factory() as session:
            return await sync_artifacts(
                session,
                source,
                target,
                dry_run=dry_run,
                concurrency=concurrency,
            )
    finally:
        await engine.dispose()
