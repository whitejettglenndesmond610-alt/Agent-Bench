from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_eval.db.enums import (
    WorkspaceAttemptFeedback,
    WorkspaceAttemptStatus,
    WorkspaceMessageRole,
)


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
    is_favorite: bool
    visibility: Literal["public", "private"]
    model: str | None


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
    visibility: Literal["public", "private"]
    model: str | None


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
    feedback: WorkspaceAttemptFeedback | None
    bookmarked: bool
    private_note: str | None
    cancel_requested_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WorkspaceAttemptError(BaseModel):
    type: str
    category: str


class WorkspaceAttemptAnnotationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback: WorkspaceAttemptFeedback | None = None
    bookmarked: bool | None = None
    private_note: str | None = Field(default=None, max_length=10_000)

    @field_validator("private_note")
    @classmethod
    def normalize_private_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        note = value.strip()
        return note or None

    @model_validator(mode="after")
    def has_change(self) -> Self:
        editable = {"feedback", "bookmarked", "private_note"}
        if not self.model_fields_set & editable:
            raise ValueError("at least one annotation field is required")
        if "bookmarked" in self.model_fields_set and self.bookmarked is None:
            raise ValueError("bookmarked cannot be null")
        return self


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


class WorkspaceComparisonCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=200_000)
    agent_version_ids: list[UUID] = Field(min_length=2, max_length=5)
    idempotency_key: UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        prompt = value.strip()
        if not prompt:
            raise ValueError("prompt cannot be blank")
        return prompt

    @field_validator("title")
    @classmethod
    def normalize_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        title = value.strip()
        if not title:
            raise ValueError("title cannot be blank")
        return title

    @model_validator(mode="after")
    def unique_agents(self) -> Self:
        if len(self.agent_version_ids) != len(set(self.agent_version_ids)):
            raise ValueError("Agent versions must be unique")
        return self


class WorkspaceComparisonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None

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
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("title cannot be null")
        if self.title is None and self.archived is None:
            raise ValueError("a title or archived state is required")
        return self


class WorkspaceComparisonCounts(BaseModel):
    queued: int
    running: int
    succeeded: int
    failed: int
    canceled: int


class WorkspaceComparisonItem(BaseModel):
    position: int
    conversation_id: UUID
    agent: WorkspaceConversationAgent
    attempt: WorkspaceAttemptResponse
    evaluation_case_id: UUID | None


class WorkspaceComparisonResponse(BaseModel):
    id: UUID
    title: str
    prompt: str
    status: Literal["queued", "running", "canceling", "completed"]
    counts: WorkspaceComparisonCounts
    revision: int
    archived_at: datetime | None
    cancel_requested_at: datetime | None
    created_at: datetime
    updated_at: datetime
    items: list[WorkspaceComparisonItem]


class WorkspaceComparisonList(BaseModel):
    items: list[WorkspaceComparisonResponse]
