import threading
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_eval.core.artifact_store import LocalArtifactStore
from agent_eval.core.settings import Settings
from agent_eval.db.base import Base, utc_now
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import ArtifactKind
from agent_eval.db.models import ArtifactIndex
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.maintenance.artifact_sync import ArtifactSyncError, sync_artifacts


async def test_artifact_sync_is_retry_safe_and_dry_run_does_not_write(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}",
        data_dir=tmp_path,
    )
    engine = create_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    source = LocalArtifactStore(tmp_path / "source")
    target = LocalArtifactStore(tmp_path / "target")
    first = source.put("runs/first.json", b"first")
    second = source.put("runs/second.json", b"second")
    try:
        async with session_scope(session_factory) as session:
            session.add_all(
                [
                    ArtifactIndex(
                        kind=ArtifactKind.AGENT_RESPONSE,
                        relative_path=stored.relative_path,
                        sha256=stored.sha256,
                        size_bytes=stored.size_bytes,
                        expires_at=utc_now() + timedelta(days=1),
                    )
                    for stored in (first, second)
                ]
            )
        async with session_factory() as session:
            dry_run = await sync_artifacts(
                session, source, target, dry_run=True, concurrency=2
            )
            assert dry_run.total == 2
            assert dry_run.copied == 0
            assert dry_run.existing == 0
            assert dry_run.pending == 2
            assert not (tmp_path / "target").exists()

            first_run = await sync_artifacts(
                session, source, target, dry_run=False, concurrency=2
            )
            assert (first_run.copied, first_run.existing, first_run.pending) == (2, 0, 0)
            retry = await sync_artifacts(
                session, source, target, dry_run=False, concurrency=2
            )
            assert (retry.copied, retry.existing, retry.pending) == (0, 2, 0)
    finally:
        await engine.dispose()


async def test_artifact_sync_rejects_corrupt_existing_target_and_keeps_source(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}",
        data_dir=tmp_path,
    )
    engine = create_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    source = LocalArtifactStore(tmp_path / "source")
    target = LocalArtifactStore(tmp_path / "target")
    stored = source.put("runs/result.json", b"source-content")
    target.put(stored.relative_path, b"different")
    try:
        async with session_scope(session_factory) as session:
            session.add(
                ArtifactIndex(
                    kind=ArtifactKind.AGENT_RESPONSE,
                    relative_path=stored.relative_path,
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    expires_at=utc_now() + timedelta(days=1),
                )
            )
        async with session_factory() as session:
            with pytest.raises(ArtifactSyncError, match="integrity verification"):
                await sync_artifacts(
                    session, source, target, dry_run=False, concurrency=1
                )
        assert source.read(
            stored.relative_path,
            expected_sha256=stored.sha256,
            expected_size=stored.size_bytes,
        ) == b"source-content"
    finally:
        await engine.dispose()


async def test_artifact_sync_missing_source_fails_without_removing_copied_objects(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}",
        data_dir=tmp_path,
    )
    engine = create_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    source = LocalArtifactStore(tmp_path / "source")
    target = LocalArtifactStore(tmp_path / "target")
    copied = source.put("runs/first.json", b"first")
    try:
        async with session_scope(session_factory) as session:
            session.add_all(
                [
                    ArtifactIndex(
                        id=UUID(int=1),
                        kind=ArtifactKind.AGENT_RESPONSE,
                        relative_path=copied.relative_path,
                        sha256=copied.sha256,
                        size_bytes=copied.size_bytes,
                        expires_at=utc_now() + timedelta(days=1),
                    ),
                    ArtifactIndex(
                        id=UUID(int=2),
                        kind=ArtifactKind.AGENT_RESPONSE,
                        relative_path="runs/missing.json",
                        sha256="0" * 64,
                        size_bytes=1,
                        expires_at=utc_now() + timedelta(days=1),
                    ),
                ]
            )
        async with session_factory() as session:
            with pytest.raises(ArtifactSyncError):
                await sync_artifacts(
                    session, source, target, dry_run=False, concurrency=1
                )
        assert target.read(
            copied.relative_path,
            expected_sha256=copied.sha256,
            expected_size=copied.size_bytes,
        ) == b"first"
    finally:
        await engine.dispose()


async def test_artifact_sync_bounds_concurrency(tmp_path: Path) -> None:
    class TrackingStore(LocalArtifactStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.active = 0
            self.maximum = 0
            self.lock = threading.Lock()

        def read(self, key: str, *, expected_sha256: str, expected_size: int) -> bytes:
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            try:
                time.sleep(0.02)
                return super().read(
                    key, expected_sha256=expected_sha256, expected_size=expected_size
                )
            finally:
                with self.lock:
                    self.active -= 1

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}",
        data_dir=tmp_path,
    )
    engine = create_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    source = TrackingStore(tmp_path / "source")
    rows = [source.put(f"runs/{number}.json", str(number).encode()) for number in range(5)]
    try:
        async with session_scope(session_factory) as session:
            session.add_all(
                [
                    ArtifactIndex(
                        kind=ArtifactKind.AGENT_RESPONSE,
                        relative_path=stored.relative_path,
                        sha256=stored.sha256,
                        size_bytes=stored.size_bytes,
                        expires_at=utc_now() + timedelta(days=1),
                    )
                    for stored in rows
                ]
            )
        async with session_factory() as session:
            await sync_artifacts(
                session,
                source,
                LocalArtifactStore(tmp_path / "target"),
                dry_run=False,
                concurrency=2,
            )
        assert source.maximum == 2
    finally:
        await engine.dispose()
