import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = Path(__file__).parents[3] / "deploy" / "backup_sets.py"
    spec = importlib.util.spec_from_file_location("backup_sets", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _complete(module: ModuleType, directory: Path, set_id: str) -> None:
    database = f"backup-set-{set_id}.postgres.dump"
    artifacts = f"backup-set-{set_id}.artifacts.tar.gz"
    (directory / database).write_bytes(set_id.encode())
    (directory / artifacts).write_bytes((set_id + "-artifacts").encode())
    module.complete(directory, set_id, database, artifacts)


def test_retention_preserves_newest_and_incomplete_work(tmp_path: Path) -> None:
    module = _module()
    old = "20000101T000000Z-1"
    newest = "20000102T000000Z-2"
    _complete(module, tmp_path, old)
    _complete(module, tmp_path, newest)
    incomplete = tmp_path / ".backup-set-current.incomplete"
    incomplete.write_bytes(b"working")

    module.retain(tmp_path, 1)

    assert not (tmp_path / f"backup-set-{old}.json").exists()
    assert (tmp_path / f"backup-set-{newest}.json").exists()
    assert incomplete.read_bytes() == b"working"


def test_verify_latest_checks_age_and_full_component_hashes(tmp_path: Path) -> None:
    module = _module()
    now = datetime.now(UTC).replace(microsecond=0)
    set_id = f"{now:%Y%m%dT%H%M%SZ}-1"
    _complete(module, tmp_path, set_id)

    verified_id, age = module.verify_latest(tmp_path, 1, now=now + timedelta(minutes=30))
    assert verified_id == set_id
    assert age == pytest.approx(0.5, abs=0.001)

    with pytest.raises(module.BackupVerificationError, match="stale"):
        module.verify_latest(tmp_path, 1, now=now + timedelta(hours=2))

    (tmp_path / f"backup-set-{set_id}.artifacts.tar.gz").write_bytes(b"corrupt")
    with pytest.raises(module.BackupVerificationError, match="corrupt"):
        module.verify_latest(tmp_path, 1, now=now)


def test_verify_latest_fails_when_no_complete_set_exists(tmp_path: Path) -> None:
    module = _module()

    with pytest.raises(module.BackupVerificationError, match="missing"):
        module.verify_latest(tmp_path, 24)
