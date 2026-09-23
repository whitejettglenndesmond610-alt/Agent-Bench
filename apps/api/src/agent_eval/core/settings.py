from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from agent_eval.core.artifact_store import (
    validate_s3_bucket,
    validate_s3_endpoint_url,
    validate_s3_prefix,
    validate_s3_region,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AGENT_EVAL_",
        extra="ignore",
    )

    environment: str = "development"
    database_url: str = "sqlite+aiosqlite:///./data/agent-eval.db"
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)
    database_pool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    data_dir: Path = Path("./data")
    log_level: str = "INFO"
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    worker_lease_seconds: int = Field(default=30, gt=0)
    worker_heartbeat_seconds: int = Field(default=10, gt=0)
    worker_stale_run_seconds: int = Field(default=60, gt=0)
    worker_max_active_runs: int = Field(default=3, ge=1, le=3)
    worker_max_local_runs: int = Field(default=3, ge=1, le=3)
    worker_max_provider_runs: int = Field(default=2, ge=1, le=3)
    worker_max_local_workspace_attempts: int = Field(default=2, ge=1, le=20)
    workspace_stream_poll_seconds: float = Field(default=0.5, gt=0, le=10)
    workspace_stream_max_seconds: float = Field(default=1800.0, gt=0, le=86400)
    worker_agent_concurrency: int = Field(default=10, gt=0, le=60)
    resource_lease_seconds: float = Field(default=30.0, ge=3, le=3600)
    resource_lease_poll_seconds: float = Field(default=0.1, gt=0, le=10)
    monitoring_expected_workers: int = Field(default=1, ge=0, le=100)
    monitoring_max_queued_runs: int = Field(default=10, ge=0, le=10000)
    monitoring_max_queue_age_minutes: int = Field(default=30, ge=1, le=10080)
    monitoring_window_hours: int = Field(default=24, ge=1, le=720)
    monitoring_max_system_failed_runs: int = Field(default=0, ge=0, le=10000)
    monitoring_resource_lease_grace_seconds: int = Field(default=120, ge=0, le=86400)
    monitoring_max_expired_resource_leases: int = Field(default=0, ge=0, le=10000)
    monitoring_max_queued_workspace_attempts: int = Field(default=20, ge=0, le=10000)
    monitoring_max_workspace_queue_age_minutes: int = Field(default=30, ge=1, le=10080)
    monitoring_max_recent_failed_workspace_attempts: int = Field(
        default=10, ge=0, le=10000
    )
    artifact_backend: Literal["local", "s3"] = "local"
    artifact_local_dir: Path | None = None
    artifact_s3_bucket: str | None = None
    artifact_s3_prefix: str = ""
    artifact_s3_endpoint_url: str | None = None
    artifact_s3_region: str | None = None
    artifact_s3_force_path_style: bool = False
    artifact_retention_days: int = Field(default=30, gt=0)
    deepseek_api_key: SecretStr | None = None
    deepseek_api_key_file: Path | None = None
    deepseek_api_url: str = "https://api.deepseek.com/chat/completions"
    # 生产环境建议把 judge 模型固定到具体版本（例如 deepseek-v4-flash-20260901），
    # 而不是漂移别名；别名升级会让同一 prompt_version 的评分不可复现。
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: int = Field(default=60, gt=0, le=600)
    deepseek_concurrency: int = Field(default=5, gt=0, le=20)
    citation_fetch_concurrency: int = Field(default=10, gt=0, le=50)
    citation_domain_concurrency: int = Field(default=2, gt=0, le=10)
    citation_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    citation_read_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    citation_max_redirects: int = Field(default=3, ge=0, le=10)
    citation_max_response_bytes: int = Field(default=2_097_152, gt=0, le=10_485_760)
    citation_max_text_bytes: int = Field(default=204_800, gt=0, le=1_048_576)
    # Interactive (tau2-bench) evaluation. The worker sets TAU2_DATA_DIR before
    # importing tau2; the API process never needs it.
    tau2_data_dir: Path | None = Field(default=None, validation_alias="TAU2_DATA_DIR")
    interactive_concurrency: int = Field(default=4, ge=1, le=20)
    interactive_timeout_seconds: int = Field(default=900, gt=0, le=7200)
    # Langfuse OTLP tracing (worker only). Compose passes these unprefixed; all
    # three must be set to enable tracing, otherwise it stays fully disabled.
    langfuse_host: str | None = Field(default=None, validation_alias="LANGFUSE_HOST")
    langfuse_public_key: str | None = Field(default=None, validation_alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: SecretStr | None = Field(
        default=None, validation_alias="LANGFUSE_SECRET_KEY"
    )
    read_only_password_hash: SecretStr | None = None
    read_only_password_hash_file: Path | None = None
    operator_password_hash: SecretStr | None = None
    operator_password_hash_file: Path | None = None
    encryption_key: SecretStr | None = None
    encryption_key_file: Path | None = None
    session_cookie_name: str = "agent_eval_session"
    csrf_cookie_name: str = "agent_eval_csrf"
    workspace_identity_cookie_name: str = "agent_eval_workspace_identity"
    workspace_identity_days: int = Field(default=365, gt=0, le=3650)
    read_only_session_hours: int = Field(default=168, gt=0)
    operator_session_hours: int = Field(default=8, gt=0)
    login_max_attempts: int = Field(default=5, gt=0)
    login_window_seconds: int = Field(default=300, gt=0)

    @field_validator(
        "read_only_password_hash",
        "operator_password_hash",
        "encryption_key",
        "deepseek_api_key",
        "langfuse_secret_key",
        mode="before",
    )
    @classmethod
    def empty_secret_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("langfuse_host", "langfuse_public_key", mode="before")
    @classmethod
    def empty_langfuse_value_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("database_url")
    @classmethod
    def supported_database_url(cls, value: str) -> str:
        driver = make_url(value).drivername
        if driver not in {"sqlite+aiosqlite", "postgresql+asyncpg"}:
            raise ValueError(
                "database URL must use sqlite+aiosqlite or postgresql+asyncpg"
            )
        return value

    @field_validator("artifact_s3_bucket")
    @classmethod
    def valid_s3_bucket(cls, value: str | None) -> str | None:
        if value == "":
            return None
        return validate_s3_bucket(value) if value is not None else None

    @field_validator("artifact_s3_prefix")
    @classmethod
    def valid_s3_prefix(cls, value: str) -> str:
        return validate_s3_prefix(value)

    @field_validator("artifact_s3_endpoint_url")
    @classmethod
    def valid_s3_endpoint(cls, value: str | None) -> str | None:
        if value == "":
            return None
        return validate_s3_endpoint_url(value) if value is not None else None

    @field_validator("artifact_s3_region")
    @classmethod
    def valid_s3_region(cls, value: str | None) -> str | None:
        if value == "":
            return None
        return validate_s3_region(value) if value is not None else None

    @model_validator(mode="after")
    def load_secret_files_and_validate_timing(self) -> Self:
        if self.artifact_local_dir is None:
            self.artifact_local_dir = self.data_dir / "artifacts"
        s3_options_set = any(
            (
                self.artifact_s3_bucket is not None,
                bool(self.artifact_s3_prefix),
                self.artifact_s3_endpoint_url is not None,
                self.artifact_s3_region is not None,
                self.artifact_s3_force_path_style,
            )
        )
        if self.artifact_backend == "s3" and self.artifact_s3_bucket is None:
            raise ValueError("artifact_s3_bucket is required for the S3 artifact backend")
        if self.artifact_backend == "local" and s3_options_set:
            raise ValueError("S3 artifact settings require artifact_backend=s3")
        for secret_name in (
            "deepseek_api_key",
            "read_only_password_hash",
            "operator_password_hash",
            "encryption_key",
        ):
            if getattr(self, secret_name) is not None:
                continue
            path = getattr(self, f"{secret_name}_file")
            if path is not None:
                value = path.read_text(encoding="utf-8").strip()
                if not value:
                    raise ValueError(f"{secret_name} file is empty")
                setattr(self, secret_name, SecretStr(value))
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("worker heartbeat interval must be shorter than lease duration")
        if self.worker_stale_run_seconds <= self.worker_lease_seconds:
            raise ValueError("stale run threshold must be longer than lease duration")
        if self.worker_max_provider_runs > self.worker_max_active_runs:
            raise ValueError("provider run limit cannot exceed active run limit")
        if self.worker_max_local_runs > self.worker_max_active_runs:
            raise ValueError("local worker run limit cannot exceed active run limit")
        if self.citation_domain_concurrency > self.citation_fetch_concurrency:
            raise ValueError("citation domain concurrency cannot exceed global concurrency")
        if self.resource_lease_poll_seconds >= self.resource_lease_seconds:
            raise ValueError("resource lease poll interval must be shorter than lease duration")
        return self

    def require_api_runtime(self) -> None:
        if self.environment != "production":
            return
        missing = [
            name
            for name in (
                "read_only_password_hash",
                "operator_password_hash",
                "encryption_key",
            )
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(f"production API secrets missing: {', '.join(missing)}")
        read_only_hash = self.read_only_password_hash
        operator_hash = self.operator_password_hash
        assert read_only_hash is not None and operator_hash is not None
        if read_only_hash.get_secret_value() == operator_hash.get_secret_value():
            raise ValueError("production passwords must use distinct hashes")
        for password_hash in (read_only_hash, operator_hash):
            if not password_hash.get_secret_value().startswith("$argon2"):
                raise ValueError("production password hashes must use Argon2")

    def require_worker_runtime(self) -> None:
        if self.environment != "production":
            return
        self.require_api_runtime()
        if self.deepseek_api_key is None:
            raise ValueError("production worker secret missing: deepseek_api_key")

    @property
    def secure_cookies(self) -> bool:
        return self.environment == "production"

    @property
    def langfuse_enabled(self) -> bool:
        return (
            self.langfuse_host is not None
            and self.langfuse_public_key is not None
            and self.langfuse_secret_key is not None
        )

    @property
    def artifact_dir(self) -> Path:
        return self.artifact_local_dir or self.data_dir / "artifacts"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"
