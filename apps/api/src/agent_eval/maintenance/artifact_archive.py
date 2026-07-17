import hashlib
import hmac
import io
import json
import os
import re
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_eval.core.artifact_store import ArtifactStore, validate_artifact_key
from agent_eval.db.enums import ArtifactKind
from agent_eval.db.models import ArtifactIndex

ARCHIVE_FORMAT = "agent-eval-artifact-archive"
ARCHIVE_VERSION = 1
MANIFEST_NAME = "manifest.json"
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_CHUNK_SIZE = 1024 * 1024
_MAX_MANIFEST_SIZE = 64 * 1024 * 1024


class ArtifactArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactArchiveReport:
    count: int
    size_bytes: int
    manifest_sha256: str
    copied: int = 0
    existing: int = 0


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii") + b"\n"


def _artifact_member_name(relative_path: str) -> str:
    validate_artifact_key(relative_path)
    return f"objects/{relative_path}"


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


async def export_artifact_archive(
    session: AsyncSession, artifact_store: ArtifactStore, output: Path
) -> ArtifactArchiveReport:
    result = await session.execute(
        select(
            ArtifactIndex.relative_path,
            ArtifactIndex.sha256,
            ArtifactIndex.size_bytes,
            ArtifactIndex.kind,
        ).order_by(ArtifactIndex.id)
    )
    artifacts: list[dict[str, Any]] = [
        {
            "relative_path": relative_path,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "kind": kind.value,
        }
        for relative_path, sha256, size_bytes, kind in result.tuples()
    ]
    manifest = {
        "format": ARCHIVE_FORMAT,
        "version": ARCHIVE_VERSION,
        "count": len(artifacts),
        "artifacts": artifacts,
    }
    manifest_bytes = _canonical_json(manifest)
    output = output.absolute()
    if not output.parent.is_dir():
        raise ArtifactArchiveError("archive output directory does not exist")

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary_path = Path(temporary_name)
    total_size = 0
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w+b", closefd=True) as raw_file:
            descriptor = -1
            with tarfile.open(fileobj=raw_file, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
                archive.addfile(
                    _tar_info(MANIFEST_NAME, len(manifest_bytes)), io.BytesIO(manifest_bytes)
                )
                for entry in artifacts:
                    content = artifact_store.read(
                        entry["relative_path"],
                        expected_sha256=entry["sha256"],
                        expected_size=entry["size_bytes"],
                    )
                    archive.addfile(
                        _tar_info(_artifact_member_name(entry["relative_path"]), len(content)),
                        io.BytesIO(content),
                    )
                    total_size += len(content)
            raw_file.flush()
            os.fsync(raw_file.fileno())
        os.replace(temporary_path, output)
        output.chmod(0o600)
        _fsync_directory(output.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise
    return ArtifactArchiveReport(
        count=len(artifacts),
        size_bytes=total_size,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def verify_artifact_archive(path: Path) -> ArtifactArchiveReport:
    count = 0
    total_size = 0
    try:
        with tarfile.open(path, mode="r|gz") as archive:
            first = archive.next()
            if first is None or first.name != MANIFEST_NAME:
                raise ArtifactArchiveError("archive manifest must be the first member")
            _validate_regular_member(first)
            if first.size > _MAX_MANIFEST_SIZE:
                raise ArtifactArchiveError("archive manifest is too large")
            manifest_file = archive.extractfile(first)
            if manifest_file is None:
                raise ArtifactArchiveError("archive manifest is unreadable")
            manifest_bytes = manifest_file.read(_MAX_MANIFEST_SIZE + 1)
            manifest = _validate_manifest(manifest_bytes)
            seen_members = {MANIFEST_NAME}
            for entry in manifest["artifacts"]:
                member = archive.next()
                if member is None:
                    raise ArtifactArchiveError("archive is missing an object member")
                _validate_regular_member(member)
                expected_name = _artifact_member_name(entry["relative_path"])
                if member.name in seen_members:
                    raise ArtifactArchiveError("archive contains a duplicate member")
                seen_members.add(member.name)
                if member.name != expected_name:
                    raise ArtifactArchiveError(
                        "archive contains an unlisted or out-of-order member"
                    )
                if member.size != entry["size_bytes"]:
                    raise ArtifactArchiveError("archive object size does not match manifest")
                body = archive.extractfile(member)
                if body is None:
                    raise ArtifactArchiveError("archive object is unreadable")
                digest = hashlib.sha256()
                size = 0
                while chunk := body.read(_CHUNK_SIZE):
                    size += len(chunk)
                    if size > entry["size_bytes"]:
                        raise ArtifactArchiveError("archive object exceeds its declared size")
                    digest.update(chunk)
                if size != entry["size_bytes"] or not hmac.compare_digest(
                    digest.hexdigest(), entry["sha256"]
                ):
                    raise ArtifactArchiveError("archive object failed integrity verification")
                count += 1
                total_size += size
            trailing = archive.next()
            if trailing is not None:
                _validate_regular_member(trailing)
                if trailing.name in seen_members:
                    raise ArtifactArchiveError("archive contains a duplicate member")
                raise ArtifactArchiveError("archive contains an unlisted trailing member")
    except ArtifactArchiveError:
        raise
    except (OSError, tarfile.TarError, UnicodeError, json.JSONDecodeError) as error:
        raise ArtifactArchiveError("artifact archive is invalid or corrupt") from error
    return ArtifactArchiveReport(
        count=count,
        size_bytes=total_size,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def restore_artifact_archive(path: Path, artifact_store: ArtifactStore) -> ArtifactArchiveReport:
    verified = verify_artifact_archive(path)
    copied = 0
    existing = 0
    with tarfile.open(path, mode="r|gz") as archive:
        manifest_member = archive.next()
        assert manifest_member is not None
        manifest_file = archive.extractfile(manifest_member)
        assert manifest_file is not None
        manifest = _validate_manifest(manifest_file.read(_MAX_MANIFEST_SIZE + 1))
        for entry in manifest["artifacts"]:
            member = archive.next()
            assert member is not None
            body = archive.extractfile(member)
            assert body is not None
            content = body.read(entry["size_bytes"] + 1)
            try:
                artifact_store.put(entry["relative_path"], content)
                copied += 1
            except FileExistsError:
                existing += 1
            artifact_store.read(
                entry["relative_path"],
                expected_sha256=entry["sha256"],
                expected_size=entry["size_bytes"],
            )
    return ArtifactArchiveReport(
        count=verified.count,
        size_bytes=verified.size_bytes,
        manifest_sha256=verified.manifest_sha256,
        copied=copied,
        existing=existing,
    )


def _validate_regular_member(member: tarfile.TarInfo) -> None:
    try:
        validate_artifact_key(member.name)
    except ValueError as error:
        raise ArtifactArchiveError("archive contains an unsafe member path") from error
    if not member.isreg():
        raise ArtifactArchiveError("archive contains a non-regular member")


def _validate_manifest(raw: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ArtifactArchiveError("archive manifest is invalid JSON") from error
    if not isinstance(manifest, dict) or set(manifest) != {
        "artifacts",
        "count",
        "format",
        "version",
    }:
        raise ArtifactArchiveError("archive manifest has an invalid schema")
    if _canonical_json(manifest) != raw:
        raise ArtifactArchiveError("archive manifest is not canonical JSON")
    if manifest["format"] != ARCHIVE_FORMAT or manifest["version"] != ARCHIVE_VERSION:
        raise ArtifactArchiveError("archive manifest version is unsupported")
    if not isinstance(manifest["count"], int) or isinstance(manifest["count"], bool):
        raise ArtifactArchiveError("archive manifest count is invalid")
    entries = manifest["artifacts"]
    if not isinstance(entries, list) or manifest["count"] != len(entries):
        raise ArtifactArchiveError("archive manifest count does not match artifacts")
    seen_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "kind",
            "relative_path",
            "sha256",
            "size_bytes",
        }:
            raise ArtifactArchiveError("archive artifact entry has an invalid schema")
        path = entry["relative_path"]
        if not isinstance(path, str):
            raise ArtifactArchiveError("archive artifact path is invalid")
        try:
            validate_artifact_key(path)
        except ValueError as error:
            raise ArtifactArchiveError("archive artifact path is unsafe") from error
        if path in seen_paths:
            raise ArtifactArchiveError("archive manifest contains a duplicate artifact path")
        seen_paths.add(path)
        if not isinstance(entry["sha256"], str) or not _HASH_PATTERN.fullmatch(entry["sha256"]):
            raise ArtifactArchiveError("archive artifact hash is invalid")
        size = entry["size_bytes"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ArtifactArchiveError("archive artifact size is invalid")
        if entry["kind"] not in {kind.value for kind in ArtifactKind}:
            raise ArtifactArchiveError("archive artifact kind is invalid")
    return manifest


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
