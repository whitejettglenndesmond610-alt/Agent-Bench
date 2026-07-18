from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from agent_eval.db.enums import WorkspaceAttemptStatus, WorkspaceMessageRole


class WorkspaceAgentResponse(BaseModel):
    agent_id: UUID
    agent_version_id: UUID
    name: str
    provider: str
    description: str | None
    logo_url: str | None
    version: int
    max_prompt_characters: int | None
    supports_citations: bool


class WorkspaceConversationCreate(BaseModel):
    agent_version_id: UUID
    title: str = Field(default="New conversation", min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("title cannot be blank")
        return title


class WorkspaceConversationPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None
    agent_version_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=0)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        title = value.strip()
        if not title:
            raise ValueError("title cannot be blank")
        return title

    @model_validator(mode="after")
    def has_change(self) -> Self:
        if self.title is None and self.archived is None and self.agent_version_id is None:
            raise ValueError("a title, archived state, or Agent version is required")
        return self


class WorkspaceConversationResponse(BaseModel):
    id: UUID
    agent_version_id: UUID
    title: str
    last_turn_no: int
    revision: int
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    agent: "WorkspaceConversationAgent"


class WorkspaceConversationAgent(BaseModel):
    agent_id: UUID
    agent_version_id: UUID
    name: str
    provider: str
    version: int


class WorkspaceConversationList(BaseModel):
    items: list[WorkspaceConversationResponse]


class WorkspaceMessageResponse(BaseModel):
    id: UUID
    turn_no: int
    role: WorkspaceMessageRole
    variant_no: int
    active: bool
    content: str
    citations: list[str]
    created_at: datetime


class WorkspaceAttemptResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID | None
    agent_version_id: UUID
    generation_no: int
    status: WorkspaceAttemptStatus
    error_category: str | None
    error: "WorkspaceAttemptError | None"
    assistant_message: WorkspaceMessageResponse | None
    http_attempt_count: int
    last_http_status: int | None
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cancel_requested_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WorkspaceAttemptError(BaseModel):
    type: str
    category: str


class WorkspaceTurnResponse(BaseModel):
    turn_no: int
    user_message: WorkspaceMessageResponse
    assistant_messages: list[WorkspaceMessageResponse]
    attempts: list[WorkspaceAttemptResponse]


class WorkspaceConversationDetail(WorkspaceConversationResponse):
    turns: list[WorkspaceTurnResponse]


class WorkspaceSendMessage(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)
    idempotency_key: UUID | None = None
    expected_revision: int = Field(ge=0)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError("message cannot be blank")
        return content


class WorkspaceRegenerateRequest(BaseModel):
    idempotency_key: UUID | None = None
    expected_revision: int = Field(ge=0)


class WorkspaceSendResult(BaseModel):
    conversation_revision: int
    user_message: WorkspaceMessageResponse
    attempt: WorkspaceAttemptResponse


class WorkspaceRegenerateResult(BaseModel):
    conversation_revision: int
    attempt: WorkspaceAttemptResponse
