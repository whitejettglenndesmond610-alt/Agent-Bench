from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from agent_eval.db.enums import PrivateAgentHealth, PrivateAgentPreset


class PrivateAgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    preset: PrivateAgentPreset
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = Field(min_length=1, max_length=4096)

    @field_validator("name", "model")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("api_key cannot be blank")
        return value


class PrivateAgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    preset: PrivateAgentPreset | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("name", "model")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        fields = {"name", "description", "preset", "model", "api_key"}
        if not self.model_fields_set & fields:
            raise ValueError("at least one change is required")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        if "preset" in self.model_fields_set and self.preset is None:
            raise ValueError("preset cannot be null")
        if "model" in self.model_fields_set and self.model is None:
            raise ValueError("model cannot be null")
        if "api_key" in self.model_fields_set and (
            self.api_key is None or not self.api_key.get_secret_value().strip()
        ):
            raise ValueError("api_key cannot be blank")
        return self


class PrivateAgentDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class PrivateAgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    agent_version_id: UUID
    active_agent_version_id: UUID | None
    active_provider: str | None
    active_model: str | None
    name: str
    description: str | None
    preset: PrivateAgentPreset
    provider: str
    model: str
    version: int
    health_status: PrivateAgentHealth
    health_checked_at: datetime | None
    health_error_code: str | None
    health_latency_ms: int | None
    workspace_available: bool
    created_at: datetime
    updated_at: datetime


class PrivateAgentList(BaseModel):
    items: list[PrivateAgentResponse]


class PrivateAgentTestResponse(PrivateAgentResponse):
    success: bool
    status_code: int | None
    latency_ms: int
    error_code: str | None
