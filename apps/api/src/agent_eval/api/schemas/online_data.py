import json
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_eval.db.enums import (
    BadPatternSeverity,
    OnlineDataEnvironment,
    OnlineDataRecordStatus,
)

MetadataValue = str | int | float | bool | None


class OnlineDataSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OnlineTraceEvent(OnlineDataSchema):
    kind: str = Field(min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=200)
    status: str | None = Field(default=None, max_length=80)
    detail: str | None = Field(default=None, max_length=4000)
    occurred_at: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0, le=3_600_000)


class OnlineDataRecordInput(OnlineDataSchema):
    external_id: str = Field(min_length=1, max_length=200)
    agent_name: str = Field(min_length=1, max_length=160)
    agent_version: str | None = Field(default=None, max_length=120)
    prompt: str = Field(min_length=1, max_length=200_000)
    answer: str = Field(min_length=1, max_length=500_000)
    occurred_at: datetime
    trace: list[OnlineTraceEvent] = Field(default_factory=list, max_length=100)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @field_validator("external_id", "agent_name", "agent_version", "prompt", "answer")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, MetadataValue]) -> dict[str, MetadataValue]:
        if len(value) > 40:
            raise ValueError("metadata must not contain more than 40 keys")
        if any(not key or len(key) > 80 for key in value):
            raise ValueError("metadata keys must contain 1-80 characters")
        if any(isinstance(item, str) and len(item) > 500 for item in value.values()):
            raise ValueError("metadata string values must not exceed 500 characters")
        if len(json.dumps(value, ensure_ascii=False)) > 20_000:
            raise ValueError("metadata JSON must not exceed 20,000 characters")
        return value

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone offset")
        return value


class OnlineDataBatchCreate(OnlineDataSchema):
    source_name: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    environment: OnlineDataEnvironment
    external_batch_id: str = Field(min_length=1, max_length=160)
    idempotency_key: UUID
    records: list[OnlineDataRecordInput] = Field(min_length=1, max_length=500)

    @field_validator("external_batch_id")
    @classmethod
    def normalize_batch_id(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def unique_external_ids(self) -> Self:
        external_ids = [record.external_id for record in self.records]
        if len(external_ids) != len(set(external_ids)):
            raise ValueError("record external_id values must be unique within a batch")
        return self


class OnlineDataBatchResponse(OnlineDataSchema):
    id: UUID
    source_name: str
    environment: OnlineDataEnvironment
    external_batch_id: str
    received_count: int
    accepted_count: int
    duplicate_count: int
    duplicate_errors: list[dict[str, str]]
    creator_display_name: str
    created_at: datetime


class OnlineDataBatchList(OnlineDataSchema):
    items: list[OnlineDataBatchResponse]


class OnlineMatchedRule(OnlineDataSchema):
    key: str
    name: str
    category: str
    severity: BadPatternSeverity
    match_count: int
    snippet: str


class OnlineDataRecordListItem(OnlineDataSchema):
    id: UUID
    batch_id: UUID
    source_name: str
    environment: OnlineDataEnvironment
    external_id: str
    agent_name: str
    agent_version: str | None
    prompt_preview: str
    answer_preview: str
    occurred_at: datetime
    status: OnlineDataRecordStatus
    risk_severity: BadPatternSeverity | None
    match_count: int
    matched_rules: list[OnlineMatchedRule]
    reviewer_display_name: str | None
    review_note: str | None
    reviewed_at: datetime | None


class OnlineDataRecordDetail(OnlineDataRecordListItem):
    prompt: str
    answer: str
    trace: list[OnlineTraceEvent]
    metadata: dict[str, MetadataValue]
    content_hash: str


class OnlineDataRecordPage(OnlineDataSchema):
    items: list[OnlineDataRecordListItem]
    next_cursor: UUID | None
    limit: int


class OnlineDataBatchReviewInput(OnlineDataSchema):
    record_ids: list[UUID] = Field(min_length=1, max_length=100)
    status: Literal[OnlineDataRecordStatus.RETAINED, OnlineDataRecordStatus.DISCARDED]
    note: str | None = Field(default=None, max_length=4000)


class OnlineDataBatchOperationResponse(OnlineDataSchema):
    requested: int
    updated: int
    skipped: int


class OnlineDataSummary(OnlineDataSchema):
    total_batches: int
    total_records: int
    pending_records: int
    risky_records: int
    retained_records: int
    discarded_records: int
    latest_received_at: datetime | None
