from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from agent_eval.db.enums import (
    Ability,
    AgentVersionLifecycle,
    DesiredRunState,
    Language,
    ResultValidityStatus,
    RunScope,
    RunStage,
    RunStatus,
)


class EvaluationRunCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    agent_version_ids: list[UUID] = Field(min_length=1, max_length=1)
    profile_key: str | None = Field(default=None, min_length=1, max_length=80)
    dataset_version_ids: list[UUID] | None = Field(default=None, min_length=1)
    judge_config_id: UUID
    score_config_id: UUID
    force_retest: bool = False

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("run name cannot be blank")
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


class EvaluationRunBatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    agent_version_ids: list[UUID] = Field(min_length=1, max_length=50)
    profile_key: str | None = Field(default=None, min_length=1, max_length=80)
    dataset_version_ids: list[UUID] | None = Field(default=None, min_length=1)
    judge_config_id: UUID
    score_config_id: UUID
    force_retest: bool = False

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("run name cannot be blank")
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


class EvaluationRunResponse(BaseModel):
    id: UUID
    name: str
    status: RunStatus
    desired_state: DesiredRunState
    stage: RunStage
    dataset_bundle_hash: str
    profile_key: str
    run_scope: RunScope
    target_ability: Ability | None
    ranking_eligible: bool
    protocol_hash: str
    judge_config_id: UUID
    score_config_id: UUID
    agent_version_ids: list[UUID]
    dataset_version_ids: list[UUID]
    question_count: int
    execution_item_count: int
    created_at: datetime


class EvaluationRunBatchResponse(BaseModel):
    runs: list[EvaluationRunResponse]


AgentMetricEvaluationStatus = Literal[
    "not_evaluated", "queued", "running", "evaluated", "needs_retest", "failed", "invalid"
]


class AgentMetricStatus(BaseModel):
    status: AgentMetricEvaluationStatus
    score: float | None = None
    run_id: UUID | None = None
    source_version: int | None = None
    updated_at: datetime | None = None
    message: str | None = None


class AgentEvaluationStatus(BaseModel):
    agent_id: UUID
    agent_version_id: UUID
    agent_name: str
    provider: str
    version: int
    max_prompt_characters: int | None
    supports_citations: bool
    comprehensive: AgentMetricStatus
    reasoning: AgentMetricStatus
    retrieval: AgentMetricStatus
    data_reasoning: AgentMetricStatus
    instruction_following: AgentMetricStatus
    long_context: AgentMetricStatus


class EvaluationRunStateResponse(BaseModel):
    id: UUID
    name: str
    status: RunStatus
    desired_state: DesiredRunState
    stage: RunStage
    profile_key: str
    run_scope: RunScope
    target_ability: Ability | None
    ranking_eligible: bool
    heartbeat_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    system_error: str | None
    created_at: datetime


class EvaluationRunListItem(EvaluationRunStateResponse):
    agent_count: int
    total: int
    completed: int
    failed: int
    retried: int


class RunAgentProgress(BaseModel):
    run_agent_id: UUID
    agent_id: UUID
    agent_version_id: UUID
    agent_name: str
    provider: str
    version: int
    total: int
    completed: int
    failed: int
    retried: int
    processed: int
    failure_categories: dict[str, int]
    validity_status: ResultValidityStatus
    validity_reason: str | None
    configuration_failure_count: int
    configuration_failure_rate: float
    result: "RunAgentResultSummary | None" = None


class RunAgentResultSummary(BaseModel):
    overall_score: float
    ability_scores: dict[Ability, float]
    answer_quality: float
    citation_quality: float
    success_rate: float
    anomaly_rate: float
    http_call_count: int
    validity_status: ResultValidityStatus = ResultValidityStatus.PENDING
    validity_reason: str | None = None
    configuration_failure_count: int = 0
    configuration_failure_rate: float = 0
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    total_tokens: int | None = None
    token_usage_available_count: int = 0
    token_usage_missing_count: int = 0
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    latency_sample_count: int = 0


class EvaluationRunProgress(EvaluationRunStateResponse):
    total: int
    completed: int
    failed: int
    retried: int
    processed: int
    progress_percent: float
    agents: list[RunAgentProgress]


class RunAgentOption(BaseModel):
    agent_id: UUID
    agent_version_id: UUID
    name: str
    provider: str
    version: int
    lifecycle_status: AgentVersionLifecycle
    max_prompt_characters: int | None
    supports_citations: bool
    profile_compatibility: list["RunProfileCompatibility"]


class RunProfileCompatibility(BaseModel):
    profile_key: str
    compatible: bool
    oversized_question_count: int
    max_question_characters: int
    citation_required_count: int
    blocking_reasons: list[str]
    warnings: list[str]


class RunDatasetOption(BaseModel):
    benchmark_id: UUID
    dataset_version_id: UUID
    name: str
    ability: Ability
    language: Language
    version: int
    question_count: int
    cohort_key: str


class RunProfileOption(BaseModel):
    key: str
    name: str
    description: str
    scope: RunScope
    target_ability: Ability | None
    dataset_version_ids: list[UUID]
    datasets: list[RunDatasetOption]
    question_count: int
    benchmark_count: int
    publishes_to: list[str]
    ranking_eligible: bool
    score_config_id: UUID | None
    ability_weights: dict[Ability, int]


class EvaluationRunOptions(BaseModel):
    agents: list[RunAgentOption]
    profiles: list[RunProfileOption]
    default_profile_key: str
    datasets: list[RunDatasetOption]
    dataset_version_ids: list[UUID]
    question_count: int
    judge_config_id: UUID | None
    judge_model: str | None
    score_config_id: UUID | None
    ability_weights: dict[Ability, int]
    ready: bool
    issues: list[str]
