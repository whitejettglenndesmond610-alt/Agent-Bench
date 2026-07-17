import hashlib
from pathlib import Path

import pytest

from agent_eval.core.artifact_store import (
    ArtifactIntegrityError,
    ArtifactKeyError,
    LocalArtifactStore,
)


def test_local_store_put_read_delete_and_probe(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root)

    store.verify_writable()
    assert list(root.iterdir()) == []

    content = b"artifact-content"
    stored = store.put("runs/run-id/responses/result.json", content)
    assert stored.relative_path == "runs/run-id/responses/result.json"
    assert stored.sha256 == hashlib.sha256(content).hexdigest()
    assert stored.size_bytes == len(content)
    assert store.read(
        stored.relative_path,
        expected_sha256=stored.sha256,
        expected_size=stored.size_bytes,
    ) == content

    assert store.delete(stored.relative_path) is True
    assert store.delete(stored.relative_path) is False
    assert list(root.iterdir()) == []


def test_local_store_refuses_overwrite(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    stored = store.put("runs/result.json", b"first")

    with pytest.raises(FileExistsError):
        store.put(stored.relative_path, b"second")

    assert store.read(
        stored.relative_path,
        expected_sha256=stored.sha256,
        expected_size=stored.size_bytes,
    ) == b"first"


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/absolute",
        "runs//result",
        "runs/./result",
        "runs/../result",
        "runs/result/",
        "runs\\result",
        "C:/result",
        "C:result",
        "runs/line\nfeed",
        "runs/null\0byte",
    ],
)
def test_local_store_rejects_noncanonical_keys(tmp_path: Path, key: str) -> None:
    store = LocalArtifactStore(tmp_path)

    with pytest.raises(ArtifactKeyError):
        store.put(key, b"content")
    with pytest.raises(ArtifactKeyError):
        store.read(key, expected_sha256="0" * 64, expected_size=0)
    with pytest.raises(ArtifactKeyError):
        store.delete(key)


def test_local_store_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    store = LocalArtifactStore(root)
    store.verify_writable()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactKeyError, match="symbolic link"):
        store.put("escape/result.json", b"content")
    with pytest.raises(ArtifactKeyError, match="symbolic link"):
        store.read("escape/result.json", expected_sha256="0" * 64, expected_size=0)
    with pytest.raises(ArtifactKeyError, match="symbolic link"):
        store.delete("escape/result.json")
    assert list(outside.iterdir()) == []

    outside_file = outside / "outside.json"
    outside_file.write_bytes(b"outside")
    (root / "linked.json").symlink_to(outside_file)
    with pytest.raises(ArtifactKeyError, match="symbolic link"):
        store.read("linked.json", expected_sha256="0" * 64, expected_size=7)
    with pytest.raises(ArtifactKeyError, match="symbolic link"):
        store.delete("linked.json")
    assert outside_file.read_bytes() == b"outside"


def test_local_store_verifies_hash_and_size(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    stored = store.put("runs/result.json", b"content")

    with pytest.raises(ArtifactIntegrityError, match="size"):
        store.read(stored.relative_path, expected_sha256=stored.sha256, expected_size=100)
    with pytest.raises(ArtifactIntegrityError, match="hash"):
        store.read(
            stored.relative_path,
            expected_sha256="0" * 64,
            expected_size=stored.size_bytes,
        )


def test_local_store_uses_private_permissions(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root)
    stored = store.put("runs/run-id/result.json", b"content")

    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "runs").stat().st_mode & 0o777 == 0o700
    assert (root / "runs" / "run-id").stat().st_mode & 0o777 == 0o700
    assert (root / stored.relative_path).stat().st_mode & 0o777 == 0o600


def test_local_store_missing_delete_is_idempotent(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)

    assert store.delete("runs/missing.json") is False
