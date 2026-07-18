from uuid import UUID, uuid4

from agent_eval.core.artifact_store import (
    ArtifactIntegrityError,
    ArtifactStore,
    StoredArtifact,
)

__all__ = [
    "ArtifactIntegrityError",
    "StoredArtifact",
    "read_indexed_artifact",
    "store_agent_response",
    "store_citation_content",
    "store_citation_judge_response",
    "store_judge_response",
    "store_workspace_response",
]


def store_agent_response(
    artifact_store: ArtifactStore,
    *,
    run_id: UUID,
    agent_id: UUID,
    question_id: UUID,
    content: bytes,
) -> StoredArtifact:
    relative_path = (
        f"runs/{run_id}/agents/{agent_id}/{question_id}/response-{uuid4()}.json"
    )
    return artifact_store.put(relative_path, content)


def store_workspace_response(
    artifact_store: ArtifactStore,
    *,
    conversation_id: UUID,
    attempt_id: UUID,
    content: bytes,
) -> StoredArtifact:
    relative_path = (
        f"workspace/conversations/{conversation_id}/attempts/{attempt_id}/response.json"
    )
    return artifact_store.put(relative_path, content)


def store_judge_response(
    artifact_store: ArtifactStore,
    *,
    run_id: UUID,
    score_id: UUID,
    content: bytes,
) -> StoredArtifact:
    relative_path = f"runs/{run_id}/judges/{score_id}/response-{uuid4()}.json"
    return artifact_store.put(relative_path, content)


def store_citation_content(
    artifact_store: ArtifactStore,
    *,
    run_id: UUID,
    citation_id: UUID,
    content: bytes,
) -> StoredArtifact:
    relative_path = f"runs/{run_id}/citations/{citation_id}/content-{uuid4()}.bin"
    return artifact_store.put(relative_path, content)


def store_citation_judge_response(
    artifact_store: ArtifactStore,
    *,
    run_id: UUID,
    citation_id: UUID,
    content: bytes,
) -> StoredArtifact:
    relative_path = (
        f"runs/{run_id}/citation-judges/{citation_id}/response-{uuid4()}.json"
    )
    return artifact_store.put(relative_path, content)


def read_indexed_artifact(
    artifact_store: ArtifactStore,
    *,
    relative_path: str,
    expected_sha256: str,
    expected_size: int,
) -> bytes:
    return artifact_store.read(
        relative_path,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
    )
