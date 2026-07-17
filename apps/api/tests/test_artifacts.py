from pathlib import Path
from uuid import uuid4

import pytest

from agent_eval.core.artifact_store import ArtifactKeyError, LocalArtifactStore
from agent_eval.core.artifacts import (
    ArtifactIntegrityError,
    read_indexed_artifact,
    store_agent_response,
    store_judge_response,
)


def test_artifacts_are_atomic_indexed_and_confined_to_root(tmp_path: Path) -> None:
    artifact_store = LocalArtifactStore(tmp_path)
    content = b'{"answer":"ok"}'
    stored = store_agent_response(
        artifact_store,
        run_id=uuid4(),
        agent_id=uuid4(),
        question_id=uuid4(),
        content=content,
    )

    assert (
        read_indexed_artifact(
            artifact_store,
            relative_path=stored.relative_path,
            expected_sha256=stored.sha256,
            expected_size=stored.size_bytes,
        )
        == content
    )

    with pytest.raises(ArtifactIntegrityError, match="hash"):
        read_indexed_artifact(
            artifact_store,
            relative_path=stored.relative_path,
            expected_sha256="0" * 64,
            expected_size=stored.size_bytes,
        )
    with pytest.raises(ArtifactKeyError):
        read_indexed_artifact(
            artifact_store,
            relative_path="../outside.json",
            expected_sha256="0" * 64,
            expected_size=0,
        )

    (tmp_path / stored.relative_path).write_bytes(b'{"answer":"no"}')
    with pytest.raises(ArtifactIntegrityError):
        read_indexed_artifact(
            artifact_store,
            relative_path=stored.relative_path,
            expected_sha256=stored.sha256,
            expected_size=stored.size_bytes,
        )


def test_judge_artifact_uses_server_generated_path(tmp_path: Path) -> None:
    artifact_store = LocalArtifactStore(tmp_path)
    run_id = uuid4()
    score_id = uuid4()
    stored = store_judge_response(
        artifact_store,
        run_id=run_id,
        score_id=score_id,
        content=b"{}",
    )

    assert stored.relative_path.startswith(f"runs/{run_id}/judges/{score_id}/response-")
    assert stored.relative_path.endswith(".json")
    assert (tmp_path / stored.relative_path).stat().st_mode & 0o777 == 0o600

    second = store_judge_response(
        artifact_store,
        run_id=run_id,
        score_id=score_id,
        content=b'{"second":true}',
    )
    assert second.relative_path != stored.relative_path
    assert read_indexed_artifact(
        artifact_store,
        relative_path=stored.relative_path,
        expected_sha256=stored.sha256,
        expected_size=stored.size_bytes,
    ) == b"{}"
