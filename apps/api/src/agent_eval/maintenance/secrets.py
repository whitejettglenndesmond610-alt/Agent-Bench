import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from agent_eval.auth.encryption import generate_encryption_key
from agent_eval.auth.security import hash_password


@dataclass(frozen=True)
class InitializedSecrets:
    directory: Path
    credentials_file: Path


def store_runtime_secret(directory: Path, filename: str, value: str) -> Path:
    if filename not in {"deepseek-api-key"}:
        raise ValueError("unsupported runtime Secret name")
    if not value.strip():
        raise ValueError("runtime Secret cannot be empty")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / filename
    if path.exists():
        raise RuntimeError(f"refusing to overwrite runtime Secret: {filename}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
        file_handle.write(value.strip() + "\n")
        file_handle.flush()
        os.fsync(file_handle.fileno())
    return path


def write_demo_environment(secret_directory: Path, output_path: Path) -> Path:
    if output_path.exists():
        raise RuntimeError(f"refusing to overwrite demo environment file: {output_path}")
    values = {
        "AGENT_EVAL_DEMO_READ_ONLY_PASSWORD_HASH": _read_secret(
            secret_directory / "read-only-password-hash"
        ),
        "AGENT_EVAL_DEMO_OPERATOR_PASSWORD_HASH": _read_secret(
            secret_directory / "operator-password-hash"
        ),
        "AGENT_EVAL_DEMO_ENCRYPTION_KEY": _read_secret(secret_directory / "encryption-key"),
    }
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
        for name, value in values.items():
            if "'" in value or "\n" in value:
                raise RuntimeError("Secret cannot be represented in the demo environment file")
            file_handle.write(f"{name}='{value}'\n")
        file_handle.flush()
        os.fsync(file_handle.fileno())
    return output_path


def write_local_real_environment(secret_directory: Path, output_path: Path) -> Path:
    if output_path.exists():
        raise RuntimeError(f"refusing to overwrite local environment file: {output_path}")
    values = {
        "AGENT_EVAL_LOCAL_READ_ONLY_PASSWORD_HASH": _read_secret(
            secret_directory / "read-only-password-hash"
        ),
        "AGENT_EVAL_LOCAL_OPERATOR_PASSWORD_HASH": _read_secret(
            secret_directory / "operator-password-hash"
        ),
        "AGENT_EVAL_LOCAL_ENCRYPTION_KEY": _read_secret(secret_directory / "encryption-key"),
        "AGENT_EVAL_LOCAL_DEEPSEEK_API_KEY": _read_secret(
            secret_directory / "deepseek-api-key"
        ),
    }
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
        for name, value in values.items():
            if "'" in value or "\n" in value:
                raise RuntimeError("Secret cannot be represented in the local environment file")
            file_handle.write(f"{name}='{value}'\n")
        file_handle.flush()
        os.fsync(file_handle.fileno())
    return output_path


def initialize_platform_secrets(directory: Path) -> InitializedSecrets:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    filenames = (
        "read-only-password-hash",
        "operator-password-hash",
        "encryption-key",
    )
    existing = [name for name in filenames if (directory / name).exists()]
    if existing:
        raise RuntimeError(f"refusing to overwrite existing Secret files: {', '.join(existing)}")

    read_only_password = secrets.token_urlsafe(18)
    operator_password = secrets.token_urlsafe(18)
    credentials_file = directory.parent / f"{directory.name}-credentials.txt"
    if credentials_file.exists():
        raise RuntimeError(f"refusing to overwrite credential handoff: {credentials_file}")
    values = {
        "read-only-password-hash": hash_password(read_only_password),
        "operator-password-hash": hash_password(operator_password),
        "encryption-key": generate_encryption_key(),
    }
    created: list[Path] = []
    try:
        for filename, value in values.items():
            path = directory / filename
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
                file_handle.write(value.rstrip("\n") + "\n")
                file_handle.flush()
                os.fsync(file_handle.fileno())
            created.append(path)
        descriptor = os.open(
            credentials_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            file_handle.write(
                f"read_only_password={read_only_password}\n"
                f"operator_password={operator_password}\n"
            )
            file_handle.flush()
            os.fsync(file_handle.fileno())
        created.append(credentials_file)
        directory_descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return InitializedSecrets(
        directory=directory,
        credentials_file=credentials_file,
    )


def _read_secret(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(f"Secret file is unavailable: {path.name}") from error
    if not value:
        raise RuntimeError(f"Secret file is empty: {path.name}")
    return value
