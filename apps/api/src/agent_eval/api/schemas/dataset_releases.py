from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_eval.db.enums import Ability, DatasetReleaseStatus, Language, QuestionType


class DatasetReleaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=10_000)
    evaluation_case_ids: list[UUID] = Field(min_length=1, max_length=500)
    idempotency_key: UUID | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be blank")
        return stripped

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        if len(self.evaluation_case_ids) != len(set(self.evaluation_case_ids)):
            raise ValueError("evaluation_case_ids must be unique")
        return self


class DatasetReleaseApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    confirm_case_selection: Literal[True]


class DatasetReleasePublish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    confirm_not_run_eligible: Literal[True]


class DatasetReleaseCaseResponse(BaseModel):
    id: UUID
    position: int
    prompt: str
    language: Language
    ability: Ability
    question_type: QuestionType
    record_hash: str


class DatasetReleaseVersionResponse(BaseModel):
    id: UUID
    benchmark_name: str
    language: Language
    ability: Ability
    version: int
    question_count: int
    content_hash: str
    run_eligible: bool


class DatasetReleaseResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    status: DatasetReleaseStatus
    revision: int
    manifest_hash: str | None
    approved_by_user_id: UUID | None
    approved_at: datetime | None
    published_by_user_id: UUID | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    cases: list[DatasetReleaseCaseResponse]
    dataset_versions: list[DatasetReleaseVersionResponse]


class DatasetReleaseList(BaseModel):
    items: list[DatasetReleaseResponse]
