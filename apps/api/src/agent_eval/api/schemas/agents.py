import re
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)

from agent_eval.agents.mapping import compile_jsonpath, parse_request_template
from agent_eval.db.enums import (
    AgentStatus,
    AgentVersionLifecycle,
    AuthType,
    PreflightStatus,
)

BEARER_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=120)
    description: str | None = None
    logo_url: HttpUrl | None = None

    @field_validator("name", "provider")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be blank")
        return stripped


class AgentVersionInput(BaseModel):
    endpoint_url: HttpUrl
    auth_type: AuthType = AuthType.NONE
    token: SecretStr | None = None
    request_template: str = Field(min_length=1, max_length=20_000)
    answer_jsonpath: str = Field(min_length=1, max_length=500)
    citations_jsonpath: str | None = Field(default=None, max_length=500)
    timeout_seconds: int = Field(default=120, ge=1, le=600)
    concurrency: int = Field(default=5, ge=1, le=20)
    max_prompt_characters: int | None = Field(default=None, ge=1, le=200_000)

    @field_validator("request_template")
    @classmethod
    def validate_request_template(cls, value: str) -> str:
        parse_request_template(value)
        return value

    @field_validator("answer_jsonpath")
    @classmethod
    def validate_answer_jsonpath(cls, value: str) -> str:
        compile_jsonpath(value)
        return value

    @field_validator("citations_jsonpath")
    @classmethod
    def validate_citations_jsonpath(cls, value: str | None) -> str | None:
        if value is not None:
            compile_jsonpath(value)
        return value

    @model_validator(mode="after")
    def validate_authentication(self) -> Self:
        if self.auth_type == AuthType.BEARER and (
            self.token is None or not self.token.get_secret_value().strip()
        ):
            raise ValueError("Bearer authentication requires a non-blank token")
        if self.auth_type == AuthType.BEARER and self.token is not None:
            token = self.token.get_secret_value()
            if BEARER_TOKEN_PATTERN.fullmatch(token) is None:
                raise ValueError("Bearer token contains invalid characters")
        if self.auth_type == AuthType.NONE and self.token is not None:
            raise ValueError("token is only valid for Bearer authentication")
        return self


class AgentVersionCreate(AgentVersionInput):
    pass


class AgentConnectionTestRequest(AgentVersionInput):
    prompt: str = Field(min_length=1, max_length=20_000)


class PersistedAgentTestRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)


class PreflightCheck(BaseModel):
    key: str
    label: str
    status: Literal["passed", "failed", "skipped"]
    prompt_characters: int
    status_code: int | None = None
    latency_ms: int | None = None
    error_category: str | None = None
    error: str | None = None


class AgentVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    version: int
    endpoint_url: str
    auth_type: AuthType
    token_configured: bool
    request_template: str
    answer_jsonpath: str
    citations_jsonpath: str | None
    timeout_seconds: int
    concurrency: int
    max_prompt_characters: int | None
    lifecycle_status: AgentVersionLifecycle
    preflight_status: PreflightStatus
    preflight_report: list[PreflightCheck]
    preflight_at: datetime | None
    enabled: bool
    created_at: datetime


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    provider: str
    description: str | None
    logo_url: str | None
    status: AgentStatus
    created_at: datetime
    updated_at: datetime


class AgentDetailResponse(AgentResponse):
    versions: list[AgentVersionResponse]


class ConnectionTestResponse(BaseModel):
    success: bool
    status_code: int | None
    latency_ms: int
    request_body: dict[str, Any]
    raw_response: Any | None
    answer: str | None
    citations: list[str]
    error: str | None


class AgentVersionPreflightResponse(BaseModel):
    version_id: UUID
    lifecycle_status: AgentVersionLifecycle
    preflight_status: PreflightStatus
    checks: list[PreflightCheck]
    preflight_at: datetime
