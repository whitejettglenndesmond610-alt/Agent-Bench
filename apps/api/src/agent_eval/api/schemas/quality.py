from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_eval.db.enums import (
    BadPatternFindingStatus,
    BadPatternSeverity,
    QualityReviewVerdict,
    QualityTaskPriority,
    QualityTaskStatus,
)


class QualitySchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BadPatternRuleCreate(QualitySchema):
    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    category: str = Field(min_length=1, max_length=80)
    severity: BadPatternSeverity
    pattern: str = Field(min_length=1, max_length=500)
    enabled: bool = True


class BadPatternRuleUpdate(QualitySchema):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    severity: BadPatternSeverity | None = None
    pattern: str | None = Field(default=None, min_length=1, max_length=500)
    enabled: bool | None = None


class BadPatternRuleResponse(QualitySchema):
    id: UUID
    key: str
    name: str
    description: str | None
    category: str
    severity: BadPatternSeverity
    pattern: str
    enabled: bool
    version: int
    finding_count: int = 0
    open_count: int = 0
    created_at: datetime
    updated_at: datetime


class QualityReviewInput(QualitySchema):
    verdict: QualityReviewVerdict
    tags: list[str] = Field(default_factory=list, max_length=12)
    note: str | None = Field(default=None, max_length=4000)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        normalized = [tag.strip() for tag in value if tag.strip()]
        if any(len(tag) > 40 for tag in normalized):
            raise ValueError("review tags must not exceed 40 characters")
        return list(dict.fromkeys(normalized))


class QualityReviewResponse(QualitySchema):
    id: UUID
    execution_item_id: UUID
    verdict: QualityReviewVerdict
    tags: list[str]
    note: str | None
    reviewer_display_name: str
    reviewed_at: datetime
    updated_at: datetime


class QualityTaskResponse(QualitySchema):
    id: UUID
    execution_item_id: UUID
    status: QualityTaskStatus
    priority: QualityTaskPriority
    assigned_to_user_id: UUID | None
    assignee_display_name: str | None
    assigned_at: datetime | None
    claimed_at: datetime | None
    completed_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class QualityReviewerResponse(QualitySchema):
    id: UUID
    username: str
    display_name: str


class QualityTaskBatchAssignInput(QualitySchema):
    finding_ids: list[UUID] = Field(min_length=1, max_length=100)
    assignee_user_id: UUID | None = None
    priority: QualityTaskPriority | None = None


class QualityFindingBatchReviewInput(QualitySchema):
    finding_ids: list[UUID] = Field(min_length=1, max_length=100)
    status: Literal[BadPatternFindingStatus.CONFIRMED, BadPatternFindingStatus.DISMISSED]
    note: str | None = Field(default=None, max_length=4000)


class QualityBatchOperationResponse(QualitySchema):
    requested: int
    updated: int
    skipped: int


class BadPatternFindingReviewInput(QualitySchema):
    status: Literal[BadPatternFindingStatus.CONFIRMED, BadPatternFindingStatus.DISMISSED]
    note: str | None = Field(default=None, max_length=4000)


class BadPatternFindingListItem(QualitySchema):
    id: UUID
    execution_item_id: UUID
    run_id: UUID
    run_name: str
    agent_name: str
    agent_version: int
    question_external_id: str
    benchmark_name: str
    rule_id: UUID
    rule_key: str
    rule_name: str
    category: str
    severity: BadPatternSeverity
    status: BadPatternFindingStatus
    match_count: int
    snippet: str
    confidence: float
    answer_preview: str | None
    error_type: str | None
    reviewer_display_name: str | None
    review_note: str | None
    reviewed_at: datetime | None
    quality_review: QualityReviewResponse | None
    task: QualityTaskResponse | None
    created_at: datetime
    updated_at: datetime


class QualityTraceEvent(QualitySchema):
    kind: Literal["request", "response", "judge", "citation", "detector", "human_review"]
    title: str
    status: str
    detail: str | None = None
    occurred_at: datetime | None = None
    latency_ms: int | None = None


class BadPatternFindingDetail(BadPatternFindingListItem):
    prompt: str
    reference_answer: str
    answer: str | None
    final_score: float | None
    judge_reason: str | None
    trace: list[QualityTraceEvent]


class QualityDashboardSummary(QualitySchema):
    total_findings: int
    open_findings: int
    critical_open_findings: int
    confirmed_findings: int
    dismissed_findings: int
    reviewed_items: int
    enabled_rules: int
    unassigned_tasks: int
    active_tasks: int
    completed_tasks: int


class QualityDashboardResponse(QualitySchema):
    summary: QualityDashboardSummary
    findings: list[BadPatternFindingListItem]
    total: int
    limit: int
    offset: int


class BadPatternRescanResponse(QualitySchema):
    execution_item_id: UUID
    finding_count: int
