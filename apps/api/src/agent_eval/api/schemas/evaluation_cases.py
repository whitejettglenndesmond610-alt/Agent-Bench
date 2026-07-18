from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_eval.db.enums import Ability, EvaluationCaseStatus, Language, QuestionType


class EvaluationCaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_attempt_id: UUID
    idempotency_key: UUID | None = None


class EvaluationCasePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    prompt: str | None = Field(default=None, min_length=1, max_length=200_000)
    reference_answer: str | None = Field(default=None, max_length=200_000)
    language: Language | None = None
    ability: Ability | None = None
    question_type: QuestionType | None = None
    requires_citations: bool | None = None
    difficulty: str | None = Field(default=None, max_length=80)
    domain: str | None = Field(default=None, max_length=120)
    review_notes: str | None = Field(default=None, max_length=10_000)

    @field_validator("prompt", "reference_answer", "difficulty", "domain", "review_notes")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def has_change(self) -> Self:
        if self.model_fields_set == {"expected_revision"}:
            raise ValueError("at least one editable field is required")
        if self.question_type == QuestionType.MULTIPLE_CHOICE:
            raise ValueError("multiple-choice drafts are not supported yet")
        return self


class EvaluationCaseReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    standalone_verified: Literal[True]
    reference_answer_verified: Literal[True]
    rights_verified: Literal[True]
    sensitive_data_verified: Literal[True]


class EvaluationCasePublish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    confirm_curated_only: Literal[True]


class EvaluationCaseSourceAgent(BaseModel):
    agent_id: UUID
    agent_version_id: UUID
    name: str
    provider: str
    version: int


class EvaluationCaseSource(BaseModel):
    attempt_id: UUID
    conversation_id: UUID
    comparison_id: UUID | None
    user_message: str
    candidate_answer: str
    citations: list[str]
    generation_no: int
    prompt_format: str
    prompt_hash: str
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    artifact_sha256: str | None
    agent: EvaluationCaseSourceAgent


class EvaluationCaseResponse(BaseModel):
    id: UUID
    status: EvaluationCaseStatus
    revision: int
    prompt: str
    reference_answer: str | None
    language: Language | None
    ability: Ability | None
    question_type: QuestionType
    requires_citations: bool
    difficulty: str | None
    domain: str | None
    review_notes: str | None
    standalone_verified: bool
    reference_answer_verified: bool
    rights_verified: bool
    sensitive_data_verified: bool
    source_hash: str
    record_hash: str | None
    reviewed_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    source: EvaluationCaseSource


class EvaluationCaseList(BaseModel):
    items: list[EvaluationCaseResponse]
