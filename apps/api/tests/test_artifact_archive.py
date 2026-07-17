import hashlib
import io
import json
import tarfile
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
from agent_eval.maintenance.artifact_archive import (
    ARCHIVE_FORMAT,
    ARCHIVE_VERSION,
    ArtifactArchiveError,
    export_artifact_archive,
    restore_artifact_archive,
    verify_artifact_archive,
)


async def _database(tmp_path: Path) -> tuple[object, object]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'archive.db'}", data_dir=tmp_path
    )
    engine = create_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, create_session_factory(engine)


async def test_artifact_archive_roundtrip_and_existing_retry(tmp_path: Path) -> None:
    engine, session_factory = await _database(tmp_path)
    source = LocalArtifactStore(tmp_path / "source")
    first = source.put("runs/one.json", b"one")
    second = source.put("citations/two.txt", b"two")
    archive = tmp_path / "artifacts.tar.gz"
    try:
        async with session_scope(session_factory) as session:  # type: ignore[operator]
            session.add_all(
                [
                    ArtifactIndex(
                        id=UUID(int=index),
                        kind=ArtifactKind.AGENT_RESPONSE,
                        relative_path=item.relative_path,
                        sha256=item.sha256,
                        size_bytes=item.size_bytes,
                        expires_at=utc_now() + timedelta(days=1),
                    )
                    for index, item in enumerate((first, second), start=1)
                ]
            )
        async with session_factory() as session:  # type: ignore[operator]
            exported = await export_artifact_archive(session, source, archive)
        assert archive.stat().st_mode & 0o777 == 0o600
        assert verify_artifact_archive(archive) == exported

        target = LocalArtifactStore(tmp_path / "target")
        restored = restore_artifact_archive(archive, target)
        assert (restored.copied, restored.existing) == (2, 0)
        retry = restore_artifact_archive(archive, target)
        assert (retry.copied, retry.existing) == (0, 2)
    finally:
        await engine.dispose()  # type: ignore[attr-defined]


async def test_export_failure_does_not_replace_output(tmp_path: Path) -> None:
    engine, session_factory = await _database(tmp_path)
    output = tmp_path / "artifacts.tar.gz"
    output.write_bytes(b"previous")
    try:
        async with session_scope(session_factory) as session:  # type: ignore[operator]
            session.add(
                ArtifactIndex(
                    kind=ArtifactKind.AGENT_RESPONSE,
                    relative_path="runs/missing.json",
                    sha256="0" * 64,
                    size_bytes=1,
                    expires_at=utc_now() + timedelta(days=1),
                )
            )
        async with session_factory() as session:  # type: ignore[operator]
            with pytest.raises(Exception, match="unavailable"):
                await export_artifact_archive(
                    session, LocalArtifactStore(tmp_path / "source"), output
                )
        assert output.read_bytes() == b"previous"
        assert not list(tmp_path.glob(".artifacts.tar.gz.*"))
    finally:
        await engine.dispose()  # type: ignore[attr-defined]


def _write_archive(path: Path, body: bytes, extra_name: str | None = None) -> None:
    manifest = {
        "artifacts": [
            {
                "kind": ArtifactKind.AGENT_RESPONSE.value,
                "relative_path": "runs/result.json",
                "sha256": hashlib.sha256(b"expected").hexdigest(),
                "size_bytes": len(b"expected"),
            }
        ],
        "count": 1,
        "format": ARCHIVE_FORMAT,
        "version": ARCHIVE_VERSION,
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(encoded)
        archive.addfile(info, io.BytesIO(encoded))
        info = tarfile.TarInfo("objects/runs/result.json")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
        if extra_name is not None:
            info = tarfile.TarInfo(extra_name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))


@pytest.mark.parametrize("extra_name", ["../escape", "objects/runs/result.json"])
def test_verify_rejects_traversal_and_duplicate_members(tmp_path: Path, extra_name: str) -> None:
    archive = tmp_path / "invalid.tar.gz"
    _write_archive(archive, b"expected", extra_name)
    with pytest.raises(ArtifactArchiveError):
        verify_artifact_archive(archive)


def test_verify_rejects_corrupt_object(tmp_path: Path) -> None:
    archive = tmp_path / "corrupt.tar.gz"
    _write_archive(archive, b"corrupt!")
    with pytest.raises(ArtifactArchiveError, match="integrity"):
        verify_artifact_archive(archive)
