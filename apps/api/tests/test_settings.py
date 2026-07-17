from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_eval.core.settings import Settings


def test_storage_paths_derive_from_data_directory(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)

    assert settings.artifact_dir == tmp_path / "artifacts"
    assert settings.artifact_local_dir == tmp_path / "artifacts"
    assert settings.artifact_backend == "local"
    assert settings.backup_dir == tmp_path / "backups"


def test_artifact_local_directory_can_be_overridden(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, artifact_local_dir=tmp_path / "objects")

    assert settings.artifact_dir == tmp_path / "objects"


def test_unknown_artifact_backend_is_rejected() -> None:
    with pytest.raises(ValidationError, match="artifact_backend"):
        Settings(artifact_backend="gcs")  # type: ignore[arg-type]


def test_s3_artifact_settings_require_bucket_and_validate_options() -> None:
    with pytest.raises(ValidationError, match="artifact_s3_bucket is required"):
        Settings(artifact_backend="s3")
    with pytest.raises(ValidationError, match="S3 artifact settings require"):
        Settings(artifact_s3_bucket="artifact-bucket")
    with pytest.raises(ValidationError, match="lowercase DNS-compatible"):
        Settings(artifact_backend="s3", artifact_s3_bucket="Invalid_Bucket")
    with pytest.raises(ValidationError, match="without credentials"):
        Settings(
            artifact_backend="s3",
            artifact_s3_bucket="artifact-bucket",
            artifact_s3_endpoint_url="http://user:password@minio:9000",
        )
    with pytest.raises(ValidationError, match="canonical"):
        Settings(
            artifact_backend="s3",
            artifact_s3_bucket="artifact-bucket",
            artifact_s3_prefix="artifacts//nested",
        )

    settings = Settings(
        artifact_backend="s3",
        artifact_s3_bucket="artifact-bucket",
        artifact_s3_prefix="production/artifacts",
        artifact_s3_endpoint_url="http://minio:9000/",
        artifact_s3_region="us-east-1",
        artifact_s3_force_path_style=True,
    )
    assert settings.artifact_s3_endpoint_url == "http://minio:9000"


def test_worker_heartbeat_must_be_shorter_than_lease() -> None:
    with pytest.raises(ValidationError, match="heartbeat interval must be shorter"):
        Settings(worker_lease_seconds=10, worker_heartbeat_seconds=10)


def test_stale_run_threshold_must_outlive_worker_lease() -> None:
    with pytest.raises(ValidationError, match="stale run threshold must be longer"):
        Settings(worker_lease_seconds=30, worker_stale_run_seconds=30)


def test_local_worker_run_limit_cannot_exceed_global_limit() -> None:
    with pytest.raises(ValidationError, match="local worker run limit"):
        Settings(
            worker_max_active_runs=1,
            worker_max_local_runs=2,
            worker_max_provider_runs=1,
        )


def test_citation_domain_concurrency_cannot_exceed_global_limit() -> None:
    with pytest.raises(ValidationError, match="domain concurrency cannot exceed global"):
        Settings(citation_fetch_concurrency=2, citation_domain_concurrency=3)


def test_resource_lease_poll_interval_must_be_shorter_than_duration() -> None:
    with pytest.raises(ValidationError, match="poll interval must be shorter"):
        Settings(resource_lease_seconds=3, resource_lease_poll_seconds=3)


def test_secret_values_can_be_loaded_from_private_files(tmp_path: Path) -> None:
    key_file = tmp_path / "encryption-key"
    key_file.write_text("secret-from-file\n")

    settings = Settings(encryption_key_file=key_file)

    assert settings.encryption_key is not None
    assert settings.encryption_key.get_secret_value() == "secret-from-file"


def test_production_runtime_fails_closed_without_required_secrets() -> None:
    settings = Settings(environment="production")

    with pytest.raises(ValueError, match="production API secrets missing"):
        settings.require_api_runtime()
    with pytest.raises(ValueError, match="production API secrets missing"):
        settings.require_worker_runtime()


def test_production_runtime_requires_distinct_argon_hashes_and_deepseek_key() -> None:
    settings = Settings(
        environment="production",
        read_only_password_hash="$argon2id$read-only",
        operator_password_hash="$argon2id$operator",
        encryption_key="encryption-key",
    )
    settings.require_api_runtime()

    with pytest.raises(ValueError, match="deepseek_api_key"):
        settings.require_worker_runtime()

    duplicate = Settings(
        environment="production",
        read_only_password_hash="$argon2id$same",
        operator_password_hash="$argon2id$same",
        encryption_key="encryption-key",
    )
    with pytest.raises(ValueError, match="distinct hashes"):
        duplicate.require_api_runtime()
