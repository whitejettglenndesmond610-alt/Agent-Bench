import hashlib
import os
from typing import Any
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from agent_eval.core.artifact_store import (
    ArtifactIntegrityError,
    ArtifactKeyError,
    S3ArtifactStore,
)


def client_error(code: str, status: int, operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "sensitive upstream message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class FakeBody:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.offset = 0
        self.bytes_returned = 0
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, amount: int) -> bytes:
        self.read_sizes.append(amount)
        result = self.content[self.offset : self.offset + amount]
        self.offset += len(result)
        self.bytes_returned += len(result)
        return result

    def close(self) -> None:
        self.closed = True


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.delete_calls: list[str] = []
        self.last_body: FakeBody | None = None
        self.content_length_override: int | None = None

    def put_object(self, **kwargs: Any) -> dict[str, object]:
        self.put_calls.append(kwargs)
        key = str(kwargs["Key"])
        if key in self.objects:
            raise client_error("PreconditionFailed", 412, "PutObject")
        self.objects[key] = (bytes(kwargs["Body"]), dict(kwargs["Metadata"]))
        return {}

    def get_object(self, **kwargs: Any) -> dict[str, object]:
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise client_error("NoSuchKey", 404, "GetObject")
        content, metadata = self.objects[key]
        self.last_body = FakeBody(content)
        return {
            "Body": self.last_body,
            "ContentLength": (
                len(content)
                if self.content_length_override is None
                else self.content_length_override
            ),
            "Metadata": metadata,
        }

    def head_object(self, **kwargs: Any) -> dict[str, object]:
        if str(kwargs["Key"]) not in self.objects:
            raise client_error("NotFound", 404, "HeadObject")
        return {}

    def delete_object(self, **kwargs: Any) -> dict[str, object]:
        key = str(kwargs["Key"])
        self.delete_calls.append(key)
        self.objects.pop(key, None)
        return {}


def make_store(client: FakeS3Client, *, prefix: str = "tenant/artifacts") -> S3ArtifactStore:
    return S3ArtifactStore(bucket="artifact-bucket", prefix=prefix, client=client)


def test_s3_put_is_conditional_and_duplicate_is_safe() -> None:
    client = FakeS3Client()
    store = make_store(client)
    content = b"first"

    stored = store.put("runs/result.json", content)

    assert client.put_calls[0]["IfNoneMatch"] == "*"
    assert client.put_calls[0]["Key"] == "tenant/artifacts/runs/result.json"
    assert client.put_calls[0]["Metadata"] == {"sha256": stored.sha256}
    with pytest.raises(FileExistsError, match="already exists") as error:
        store.put("runs/result.json", b"second")
    assert "sensitive" not in str(error.value)
    assert client.objects["tenant/artifacts/runs/result.json"][0] == content


def test_s3_read_verifies_metadata_size_hash_and_missing() -> None:
    client = FakeS3Client()
    store = make_store(client)
    stored = store.put("runs/result.json", b"content")

    assert store.read(
        stored.relative_path,
        expected_sha256=stored.sha256,
        expected_size=stored.size_bytes,
    ) == b"content"
    assert client.last_body is not None and client.last_body.closed

    client.objects["tenant/artifacts/runs/result.json"][1]["sha256"] = "0" * 64
    with pytest.raises(ArtifactIntegrityError, match="metadata hash"):
        store.read(
            stored.relative_path,
            expected_sha256=stored.sha256,
            expected_size=stored.size_bytes,
        )
    assert client.last_body is not None and client.last_body.closed

    client.objects["tenant/artifacts/runs/result.json"][1]["sha256"] = stored.sha256
    client.objects["tenant/artifacts/runs/result.json"] = (
        b"CONTENT",
        {"sha256": stored.sha256},
    )
    with pytest.raises(ArtifactIntegrityError, match="artifact hash"):
        store.read(
            stored.relative_path,
            expected_sha256=stored.sha256,
            expected_size=stored.size_bytes,
        )

    client.objects["tenant/artifacts/runs/result.json"] = (
        b"content",
        {"sha256": stored.sha256},
    )
    with pytest.raises(ArtifactIntegrityError, match="hash"):
        store.read(
            stored.relative_path,
            expected_sha256="0" * 64,
            expected_size=stored.size_bytes,
        )
    with pytest.raises(ArtifactIntegrityError, match="unavailable"):
        store.read("runs/missing.json", expected_sha256="0" * 64, expected_size=0)


def test_s3_read_rejects_content_length_before_read_and_bounds_body() -> None:
    client = FakeS3Client()
    store = make_store(client)
    stored = store.put("runs/result.json", b"content")
    client.content_length_override = stored.size_bytes + 1

    with pytest.raises(ArtifactIntegrityError, match="size"):
        store.read(
            stored.relative_path,
            expected_sha256=stored.sha256,
            expected_size=stored.size_bytes,
        )
    assert client.last_body is not None
    assert client.last_body.read_sizes == []
    assert client.last_body.closed

    client.content_length_override = stored.size_bytes
    client.objects["tenant/artifacts/runs/result.json"] = (
        b"content-extra",
        {"sha256": stored.sha256},
    )
    with pytest.raises(ArtifactIntegrityError, match="size"):
        store.read(
            stored.relative_path,
            expected_sha256=stored.sha256,
            expected_size=stored.size_bytes,
        )
    assert client.last_body is not None
    assert client.last_body.bytes_returned <= stored.size_bytes + 1
    assert client.last_body.closed


def test_s3_delete_is_idempotent_and_probe_cleans_prefixed_key() -> None:
    client = FakeS3Client()
    store = make_store(client)
    stored = store.put("runs/result.json", b"content")

    assert store.delete(stored.relative_path) is True
    assert store.delete(stored.relative_path) is False
    store.verify_writable()

    assert client.objects == {}
    assert client.put_calls[-1]["Key"].startswith("tenant/artifacts/.artifact-probes/")
    assert client.delete_calls[-1] == client.put_calls[-1]["Key"]


@pytest.mark.parametrize("key", ["../escape", "/absolute", "runs//result", "runs\\result"])
def test_s3_rejects_unsafe_relative_keys(key: str) -> None:
    store = make_store(FakeS3Client())

    with pytest.raises(ArtifactKeyError):
        store.put(key, b"content")


@pytest.mark.minio
def test_real_s3_contract_when_configured() -> None:
    endpoint = os.getenv("AGENT_EVAL_TEST_S3_ENDPOINT_URL")
    bucket = os.getenv("AGENT_EVAL_TEST_S3_BUCKET")
    if not endpoint or not bucket:
        pytest.skip("real S3/MinIO contract is not configured")
    store = S3ArtifactStore(
        bucket=bucket,
        prefix=f"contract/{uuid4().hex}",
        endpoint_url=endpoint,
        region=os.getenv("AGENT_EVAL_TEST_S3_REGION", "us-east-1"),
        force_path_style=True,
    )
    key = "runs/result.json"
    content = b"real-contract"
    digest = hashlib.sha256(content).hexdigest()
    try:
        store.verify_writable()
        stored = store.put(key, content)
        assert stored.sha256 == digest
        assert store.read(key, expected_sha256=digest, expected_size=len(content)) == content
        with pytest.raises(FileExistsError):
            store.put(key, b"replacement")
        assert store.delete(key) is True
        assert store.delete(key) is False
    finally:
        store.delete(key)
