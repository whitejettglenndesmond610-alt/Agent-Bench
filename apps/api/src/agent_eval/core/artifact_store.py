"""Backend-neutral artifact storage.

The interface is synchronous by design. Worker call sites already move artifact I/O to
``asyncio.to_thread``, which keeps adapters simple and avoids imposing an async SDK on every
backend.
"""

import errno
import hashlib
import hmac
import ipaddress
import os
import re
import stat
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from agent_eval.core.settings import Settings


@dataclass(frozen=True)
class StoredArtifact:
    relative_path: str
    sha256: str
    size_bytes: int


class ArtifactIntegrityError(RuntimeError):
    pass


class ArtifactKeyError(ValueError):
    pass


class ArtifactStore(Protocol):
    """Synchronous, create-only storage for immutable artifact objects."""

    def put(self, key: str, content: bytes) -> StoredArtifact:
        """Create an object, failing if the key already exists."""

    def read(self, key: str, *, expected_sha256: str, expected_size: int) -> bytes:
        """Read an object within the expected bound and verify its index metadata."""

    def delete(self, key: str) -> bool:
        """Delete an object, returning whether it existed."""

    def verify_writable(self) -> None:
        """Create, sync, and remove a private probe object."""


def validate_artifact_key(key: str) -> tuple[str, ...]:
    """Validate and split a canonical relative POSIX object key."""
    if not key or key.startswith("/"):
        raise ArtifactKeyError("artifact key must be a non-empty relative POSIX path")
    if "\\" in key:
        raise ArtifactKeyError("artifact key must use POSIX separators")
    if len(key) >= 2 and key[0].isalpha() and key[1] == ":":
        raise ArtifactKeyError("artifact key must not contain a Windows drive")
    if any(unicodedata.category(character) == "Cc" for character in key):
        raise ArtifactKeyError("artifact key must not contain control characters")
    parts = tuple(key.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ArtifactKeyError("artifact key is not canonical")
    return parts


def validate_s3_bucket(bucket: str) -> str:
    if not 3 <= len(bucket) <= 63:
        raise ValueError("S3 bucket must contain 3 to 63 characters")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", bucket):
        raise ValueError("S3 bucket must use lowercase DNS-compatible characters")
    if ".." in bucket or ".-" in bucket or "-." in bucket:
        raise ValueError("S3 bucket is not canonical")
    try:
        ipaddress.ip_address(bucket)
    except ValueError:
        return bucket
    raise ValueError("S3 bucket must not be an IP address")


def validate_s3_prefix(prefix: str) -> str:
    if prefix == "":
        return prefix
    validate_artifact_key(prefix)
    if len(prefix.encode("utf-8")) > 512:
        raise ValueError("S3 prefix is too long")
    return prefix


def validate_s3_endpoint_url(endpoint_url: str) -> str:
    parsed = urlsplit(endpoint_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("S3 endpoint URL must be an HTTP(S) origin without credentials")
    return endpoint_url.rstrip("/")


def validate_s3_region(region: str) -> str:
    if not region or len(region) > 64 or not re.fullmatch(r"[A-Za-z0-9-]+", region):
        raise ValueError("S3 region is invalid")
    return region


class LocalArtifactStore:
    """Filesystem artifact storage confined beneath one non-symlink root."""

    _directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def put(self, key: str, content: bytes) -> StoredArtifact:
        parts = validate_artifact_key(key)
        parent_fd = self._open_directory(parts[:-1], create=True)
        temporary_name = f".artifact-{uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            os.fchmod(descriptor, 0o600)
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None

            # Linking a same-directory temporary file publishes atomically while retaining the
            # create-only guarantee that os.replace cannot provide under concurrent writers.
            os.link(
                temporary_name,
                parts[-1],
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_fd)
            raise
        finally:
            os.close(parent_fd)

        return StoredArtifact(
            relative_path=key,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )

    def read(self, key: str, *, expected_sha256: str, expected_size: int) -> bytes:
        parts = validate_artifact_key(key)
        if expected_size < 0:
            raise ArtifactIntegrityError("artifact size is invalid")
        try:
            parent_fd = self._open_directory(parts[:-1], create=False)
        except FileNotFoundError as error:
            raise ArtifactIntegrityError("artifact file is unavailable") from error
        try:
            try:
                metadata = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ArtifactKeyError("artifact key traverses a symbolic link")
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            except ArtifactKeyError:
                raise
            except OSError as error:
                raise ArtifactIntegrityError("artifact file is unavailable") from error
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ArtifactIntegrityError("artifact object is not a regular file")
                if metadata.st_size != expected_size:
                    raise ArtifactIntegrityError("artifact size does not match index")
                digest = hashlib.sha256()
                chunks: list[bytes] = []
                remaining = expected_size
                while remaining:
                    chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ArtifactIntegrityError("artifact size does not match index")
                    remaining -= len(chunk)
                    digest.update(chunk)
                    chunks.append(chunk)
                if os.read(descriptor, 1):
                    raise ArtifactIntegrityError("artifact size does not match index")
                if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
                    raise ArtifactIntegrityError("artifact hash does not match index")
                return b"".join(chunks)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)

    def delete(self, key: str) -> bool:
        parts = validate_artifact_key(key)
        try:
            parent_fd = self._open_directory(parts[:-1], create=False)
        except FileNotFoundError:
            return False
        try:
            try:
                metadata = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(metadata.st_mode):
                raise ArtifactKeyError("artifact key traverses a symbolic link")
            if not stat.S_ISREG(metadata.st_mode):
                raise ArtifactIntegrityError("artifact object is not a regular file")
            os.unlink(parts[-1], dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        self._prune_empty_directories(parts[:-1])
        return True

    def verify_writable(self) -> None:
        root_fd = self._open_directory((), create=True)
        probe_name = f".ready-{uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                probe_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_fd,
            )
            os.write(descriptor, b"ready")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.unlink(probe_name, dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                os.unlink(probe_name, dir_fd=root_fd)
            os.close(root_fd)

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.root.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ArtifactKeyError("artifact root must be a non-symlink directory")
        self.root.chmod(0o700)

    def _open_directory(self, parts: tuple[str, ...], *, create: bool) -> int:
        self._ensure_root()
        descriptor = os.open(self.root, self._directory_flags)
        try:
            for part in parts:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                try:
                    child = os.open(part, self._directory_flags, dir_fd=descriptor)
                except OSError as error:
                    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise ArtifactKeyError(
                            "artifact key traverses a symbolic link or non-directory"
                        ) from error
                    raise
                os.fchmod(child, 0o700)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _prune_empty_directories(self, parts: tuple[str, ...]) -> None:
        for depth in range(len(parts), 0, -1):
            try:
                parent_fd = self._open_directory(parts[: depth - 1], create=False)
            except (FileNotFoundError, ArtifactKeyError):
                return
            try:
                os.rmdir(parts[depth - 1], dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError as error:
                if error.errno in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT}:
                    return
                raise
            finally:
                os.close(parent_fd)


class S3ArtifactStore:
    """S3-compatible immutable storage using SDK credential discovery."""

    _chunk_size = 1024 * 1024

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        force_path_style: bool = False,
        client: Any | None = None,
    ) -> None:
        if not isinstance(force_path_style, bool):
            raise ValueError("S3 force_path_style must be a boolean")
        self.bucket = validate_s3_bucket(bucket)
        self.prefix = validate_s3_prefix(prefix)
        self.endpoint_url = (
            validate_s3_endpoint_url(endpoint_url) if endpoint_url is not None else None
        )
        self.region = validate_s3_region(region) if region is not None else None
        self.force_path_style = force_path_style
        self._client = client or boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            config=Config(
                s3={"addressing_style": "path" if self.force_path_style else "auto"}
            ),
        )

    def put(self, key: str, content: bytes) -> StoredArtifact:
        object_key = self._object_key(key)
        digest = hashlib.sha256(content).hexdigest()
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=content,
                ContentLength=len(content),
                Metadata={"sha256": digest},
                IfNoneMatch="*",
            )
        except ClientError as error:
            if _is_precondition_failure(error):
                raise FileExistsError("artifact object already exists") from None
            raise RuntimeError("artifact storage request failed") from None
        except BotoCoreError:
            raise RuntimeError("artifact storage request failed") from None
        return StoredArtifact(relative_path=key, sha256=digest, size_bytes=len(content))

    def read(self, key: str, *, expected_sha256: str, expected_size: int) -> bytes:
        object_key = self._object_key(key)
        if expected_size < 0:
            raise ArtifactIntegrityError("artifact size is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ArtifactIntegrityError("artifact hash is invalid")
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=object_key)
        except ClientError as error:
            if _is_missing(error):
                raise ArtifactIntegrityError("artifact object is unavailable") from None
            raise RuntimeError("artifact storage request failed") from None
        except BotoCoreError:
            raise RuntimeError("artifact storage request failed") from None

        body = response.get("Body")
        if body is None or not callable(getattr(body, "read", None)):
            raise ArtifactIntegrityError("artifact object has no readable body")
        try:
            try:
                if response.get("ContentLength") != expected_size:
                    raise ArtifactIntegrityError("artifact size does not match index")
                metadata = response.get("Metadata")
                stored_sha256 = metadata.get("sha256") if isinstance(metadata, dict) else None
                if not isinstance(stored_sha256, str) or not hmac.compare_digest(
                    stored_sha256, expected_sha256
                ):
                    raise ArtifactIntegrityError("artifact metadata hash does not match index")

                digest = hashlib.sha256()
                chunks: list[bytes] = []
                remaining = expected_size + 1
                size = 0
                while remaining:
                    chunk = body.read(min(remaining, self._chunk_size))
                    if not isinstance(chunk, bytes):
                        raise ArtifactIntegrityError("artifact body is invalid")
                    if not chunk:
                        break
                    if len(chunk) > remaining:
                        raise ArtifactIntegrityError("artifact size does not match index")
                    size += len(chunk)
                    remaining -= len(chunk)
                    digest.update(chunk)
                    chunks.append(chunk)
                if size != expected_size:
                    raise ArtifactIntegrityError("artifact size does not match index")
                if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
                    raise ArtifactIntegrityError("artifact hash does not match index")
                return b"".join(chunks)
            except BotoCoreError:
                raise RuntimeError("artifact storage request failed") from None
        finally:
            with suppress(Exception):
                body.close()

    def delete(self, key: str) -> bool:
        object_key = self._object_key(key)
        try:
            self._client.head_object(Bucket=self.bucket, Key=object_key)
        except ClientError as error:
            if _is_missing(error):
                return False
            raise RuntimeError("artifact storage request failed") from None
        except BotoCoreError:
            raise RuntimeError("artifact storage request failed") from None
        try:
            self._client.delete_object(Bucket=self.bucket, Key=object_key)
        except (BotoCoreError, ClientError):
            raise RuntimeError("artifact storage request failed") from None
        return True

    def verify_writable(self) -> None:
        key = f".artifact-probes/{uuid4().hex}"
        try:
            self.put(key, b"ready")
        finally:
            self.delete(key)

    def _object_key(self, key: str) -> str:
        validate_artifact_key(key)
        object_key = f"{self.prefix}/{key}" if self.prefix else key
        if len(object_key.encode("utf-8")) > 1024:
            raise ArtifactKeyError("artifact key is too long for S3")
        return object_key


def _is_precondition_failure(error: ClientError) -> bool:
    response = error.response
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"PreconditionFailed", "ConditionalRequestConflict"} or status in {409, 412}


def _is_missing(error: ClientError) -> bool:
    response = error.response
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"NoSuchKey", "NotFound", "404"} or status == 404


def create_artifact_store(settings: "Settings") -> ArtifactStore:
    if settings.artifact_backend == "local":
        return LocalArtifactStore(settings.artifact_dir)
    if settings.artifact_backend == "s3":
        assert settings.artifact_s3_bucket is not None
        return S3ArtifactStore(
            bucket=settings.artifact_s3_bucket,
            prefix=settings.artifact_s3_prefix,
            endpoint_url=settings.artifact_s3_endpoint_url,
            region=settings.artifact_s3_region,
            force_path_style=settings.artifact_s3_force_path_style,
        )
    raise ValueError(f"unsupported artifact backend: {settings.artifact_backend}")
