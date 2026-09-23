from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from agent_eval.api.schemas.runs import RunAgentResultSummary
from agent_eval.db.enums import Ability, RunScope, RunStage, RunStatus


class EvaluationExperimentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    idempotency_key: UUID
    agent_version_ids: list[UUID] = Field(min_length=1, max_length=50)
    profile_key: str | None = Field(default=None, min_length=1, max_length=80)
    dataset_version_ids: list[UUID] | None = Field(default=None, min_length=1)
    judge_config_id: UUID
    score_config_id: UUID | None = None
    force_retest: bool = False

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("experiment name cannot be blank")
        return stripped

    @model_validator(mode="after")
    def validate_unique_selections(self) -> Self:
        if len(self.agent_version_ids) != len(set(self.agent_version_ids)):
            raise ValueError("agent version selections must be unique")
        if self.dataset_version_ids is not None and len(self.dataset_version_ids) != len(
            set(self.dataset_version_ids)
        ):
            raise ValueError("dataset version selections must be unique")
        return self


class EvaluationExperimentCreator(BaseModel):
    user_id: UUID | None
    subject_key: str
    display_name: str


class EvaluationExperimentRunCounts(BaseModel):
    draft: int = 0
    queued: int = 0
    running: int = 0
    paused: int = 0
    scoring: int = 0
    completed: int = 0
    published: int = 0
    system_failed: int = 0
    canceled: int = 0


class EvaluationExperimentSummary(BaseModel):
    id: UUID
    name: str
    creator: EvaluationExperimentCreator
    status: RunStatus
    protocol_hash: str
    profile_key: str
    run_scope: RunScope
    target_ability: Ability | None
    member_count: int
    run_counts: EvaluationExperimentRunCounts
    total: int
    processed: int
    completed: int
    failed: int
    progress_percent: float
    created_at: datetime
    updated_at: datetime


class EvaluationExperimentMember(BaseModel):
    position: int
    run_id: UUID
    run_name: str
    status: RunStatus
    stage: RunStage
    agent_id: UUID
    agent_version_id: UUID
    agent_name: str
    provider: str
    version: int
    total: int
    processed: int
    completed: int
    failed: int
    retried: int
    progress_percent: float
    result: RunAgentResultSummary | None
    metric_scores: dict[str, float]
    metric_ranks: dict[str, int]


class EvaluationExperimentMetricComparison(BaseModel):
    metric: str
    protocol_hash: str
    compared_count: int


class EvaluationExperimentDetail(EvaluationExperimentSummary):
    members: list[EvaluationExperimentMember]
    comparison_metrics: list[EvaluationExperimentMetricComparison]
