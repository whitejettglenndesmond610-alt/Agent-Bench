#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_SET_ID = re.compile(r"(?P<timestamp>[0-9]{8}T[0-9]{6}Z)-[0-9]+")
_MANIFEST = re.compile(r"backup-set-(?P<set_id>[0-9]{8}T[0-9]{6}Z-[0-9]+)\.json")


class BackupVerificationError(ValueError):
    def __init__(self, status: str, set_id: str = "unknown", age_hours: str = "unknown") -> None:
        super().__init__(status)
        self.status = status
        self.set_id = set_id
        self.age_hours = age_hours


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _regular_file(directory: Path, name: str) -> Path:
    if Path(name).name != name or name in {".", ".."}:
        raise ValueError("backup component filename is unsafe")
    path = directory / name
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("backup component is not a regular file")
    return path


def complete(directory: Path, set_id: str, database_name: str, artifacts_name: str) -> None:
    if _SET_ID.fullmatch(set_id) is None:
        raise ValueError("backup set ID is invalid")
    expected_database = f"backup-set-{set_id}.postgres.dump"
    expected_artifacts = f"backup-set-{set_id}.artifacts.tar.gz"
    if database_name != expected_database or artifacts_name != expected_artifacts:
        raise ValueError("backup component names do not match set ID")
    database_path = _regular_file(directory, database_name)
    artifacts_path = _regular_file(directory, artifacts_name)
    _fsync_file(database_path)
    _fsync_file(artifacts_path)
    database_hash, database_size = _hash_file(database_path)
    artifacts_hash, artifacts_size = _hash_file(artifacts_path)
    manifest: dict[str, Any] = {
        "artifacts": {
            "filename": artifacts_name,
            "sha256": artifacts_hash,
            "size_bytes": artifacts_size,
        },
        "completed_at": datetime.now(UTC).isoformat(),
        "database": {
            "filename": database_name,
            "sha256": database_hash,
            "size_bytes": database_size,
        },
        "format": "agent-eval-backup-set",
        "set_id": set_id,
        "version": 1,
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    output = directory / f"backup-set-{set_id}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        _fsync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"database_sha256={database_hash} artifacts_sha256={artifacts_hash}")


def _load_complete(directory: Path, manifest_path: Path) -> tuple[datetime, datetime, list[Path]]:
    match = _MANIFEST.fullmatch(manifest_path.name)
    if match is None or not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("backup set manifest filename is invalid")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or set(manifest) != {
        "artifacts",
        "completed_at",
        "database",
        "format",
        "set_id",
        "version",
    }:
        raise ValueError("backup set manifest schema is invalid")
    set_id = match.group("set_id")
    if manifest["format"] != "agent-eval-backup-set" or manifest["version"] != 1:
        raise ValueError("backup set manifest version is invalid")
    if manifest["set_id"] != set_id:
        raise ValueError("backup set manifest ID does not match filename")
    try:
        completed_at = datetime.fromisoformat(manifest["completed_at"])
    except (TypeError, ValueError) as error:
        raise ValueError("backup set completion time is invalid") from error
    if completed_at.tzinfo is None:
        raise ValueError("backup set completion time is invalid")
    set_match = _SET_ID.fullmatch(set_id)
    assert set_match is not None
    timestamp = datetime.strptime(
        set_match.group("timestamp"), "%Y%m%dT%H%M%SZ"
    ).replace(tzinfo=UTC)
    components: list[Path] = []
    for key, suffix in (("database", ".postgres.dump"), ("artifacts", ".artifacts.tar.gz")):
        item = manifest[key]
        if not isinstance(item, dict) or set(item) != {"filename", "sha256", "size_bytes"}:
            raise ValueError("backup set component schema is invalid")
        expected = f"backup-set-{set_id}{suffix}"
        if item["filename"] != expected:
            raise ValueError("backup set component filename is invalid")
        path = _regular_file(directory, expected)
        digest, size = _hash_file(path)
        if digest != item["sha256"] or size != item["size_bytes"]:
            raise ValueError("backup set component integrity check failed")
        components.append(path)
    return timestamp, completed_at.astimezone(UTC), components


def retain(directory: Path, days: int) -> None:
    if days < 1 or days > 36500:
        raise ValueError("retention days must be between 1 and 36500")
    sets: list[tuple[datetime, Path, list[Path]]] = []
    for path in directory.iterdir():
        if _MANIFEST.fullmatch(path.name):
            timestamp, _, components = _load_complete(directory, path)
            sets.append((timestamp, path, components))
    sets.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    removed = 0
    for timestamp, manifest_path, components in sets[1:]:
        if timestamp >= cutoff:
            continue
        for component in components:
            component.unlink()
        manifest_path.unlink()
        removed += 1
    if removed:
        _fsync_directory(directory)
    print(f"complete_sets={len(sets) - removed} removed={removed}")


def verify_latest(
    directory: Path, max_age_hours: float, *, now: datetime | None = None
) -> tuple[str, float]:
    if not 0 < max_age_hours <= 8760:
        raise ValueError("maximum backup age must be between 0 and 8760 hours")
    candidates: list[tuple[datetime, str, Path]] = []
    for path in directory.iterdir():
        match = _MANIFEST.fullmatch(path.name)
        if match is None:
            continue
        set_id = match.group("set_id")
        set_match = _SET_ID.fullmatch(set_id)
        assert set_match is not None
        timestamp = datetime.strptime(
            set_match.group("timestamp"), "%Y%m%dT%H%M%SZ"
        ).replace(tzinfo=UTC)
        candidates.append((timestamp, set_id, path))
    if not candidates:
        raise BackupVerificationError("missing")
    _, set_id, manifest_path = max(candidates, key=lambda item: (item[0], item[1]))
    current_time = now or datetime.now(UTC)
    try:
        _, completed_at, _ = _load_complete(directory, manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise BackupVerificationError("corrupt", set_id) from error
    age_hours = max(0.0, (current_time.astimezone(UTC) - completed_at).total_seconds() / 3600)
    rendered_age = f"{age_hours:.2f}"
    if age_hours > max_age_hours:
        raise BackupVerificationError("stale", set_id, rendered_age)
    return set_id, age_hours


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage production backup set manifests")
    subparsers = parser.add_subparsers(dest="command", required=True)
    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("directory", type=Path)
    complete_parser.add_argument("set_id")
    complete_parser.add_argument("database_name")
    complete_parser.add_argument("artifacts_name")
    retain_parser = subparsers.add_parser("retain")
    retain_parser.add_argument("directory", type=Path)
    retain_parser.add_argument("days", type=int)
    verify_parser = subparsers.add_parser("verify-latest")
    verify_parser.add_argument("directory", type=Path)
    verify_parser.add_argument("max_age_hours", type=float)
    arguments = parser.parse_args()
    directory = arguments.directory.resolve(strict=True)
    if not directory.is_dir():
        parser.error("backup path is not a directory")
    try:
        if arguments.command == "complete":
            complete(directory, arguments.set_id, arguments.database_name, arguments.artifacts_name)
        elif arguments.command == "retain":
            retain(directory, arguments.days)
        else:
            set_id, age_hours = verify_latest(directory, arguments.max_age_hours)
            print(f"backup_set={set_id} age_hours={age_hours:.2f} status=ok")
    except BackupVerificationError as error:
        parser.exit(
            1,
            f"backup_set={error.set_id} age_hours={error.age_hours} status={error.status}\n",
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        del error
        parser.exit(1, "backup_set=unknown age_hours=unknown status=error\n")


if __name__ == "__main__":
    main()
