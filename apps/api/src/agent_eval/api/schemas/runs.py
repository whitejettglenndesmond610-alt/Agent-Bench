from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_eval.db.enums import (
    Ability,
    AgentVersionLifecycle,
    ArtifactKind,
    AttemptStatus,
    CitationFetchStatus,
    DesiredRunState,
    ExecutionStatus,
    Language,
    QuestionType,
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
    trial_count: int | None = Field(default=None, ge=1, le=10)

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
    trial_count: int | None = Field(default=None, ge=1, le=10)

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
    experiment_id: UUID | None
    experiment_position: int | None
    name: str
    status: RunStatus
    desired_state: DesiredRunState
    stage: RunStage
    dataset_bundle_hash: str
    profile_key: str
    run_scope: RunScope
    target_ability: Ability | None
    ranking_eligible: bool
    trial_count: int
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
    experiment_id: UUID | None
    experiment_position: int | None
    name: str
    status: RunStatus
    desired_state: DesiredRunState
    stage: RunStage
    profile_key: str
    run_scope: RunScope
    target_ability: Ability | None
    ranking_eligible: bool
    trial_count: int
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
    pass_at_1: float | None = None
    pass_at_k: float | None = None
    pass_pow_k: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    total_cost_usd: float | None = None
    cost_per_task_usd: float | None = None
    input_token_share: float | None = None
    output_token_share: float | None = None


class EvaluationRunProgress(EvaluationRunStateResponse):
    total: int
    completed: int
    failed: int
    retried: int
    processed: int
    progress_percent: float
    agents: list[RunAgentProgress]


class RunEvidenceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RunEvidenceQuestionChoice(RunEvidenceSchema):
    label: str
    text: str


class RunEvidenceArtifactMetadata(RunEvidenceSchema):
    artifact_id: UUID
    kind: ArtifactKind
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    expires_at: datetime
    retained: bool


class RunEvidenceScoreSummary(RunEvidenceSchema):
    score_id: UUID
    answer_score: float = Field(ge=0, le=100)
    citation_score: float | None = Field(default=None, ge=0, le=100)
    final_score: float = Field(ge=0, le=100)


class RunEvidenceScoreDetail(RunEvidenceScoreSummary):
    judge_reason: str | None
    judge_artifact: RunEvidenceArtifactMetadata | None
    judge_provider: str | None = None
    judge_model_requested: str | None = None
    judge_model_returned: str | None = None
    judge_prompt_version: str | None = None
    judge_temperature: float | None = None
    judge_details: dict[str, Any] | None = None


class RunEvidenceListItem(RunEvidenceSchema):
    execution_item_id: UUID
    run_agent_id: UUID
    agent_id: UUID
    agent_version_id: UUID
    agent_name: str
    provider: str
    version: int
    question_id: UUID
    question_external_id: str
    question_type: QuestionType
    benchmark_id: UUID
    benchmark_name: str
    ability: Ability
    language: Language
    requires_citations: bool
    difficulty: str | None
    domain: str | None
    status: ExecutionStatus
    error_type: str | None
    answer_preview: str | None
    attempt_count: int = Field(ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    score: RunEvidenceScoreSummary | None
    created_at: datetime
    updated_at: datetime


class EvaluationRunEvidenceList(RunEvidenceSchema):
    run_id: UUID
    items: list[RunEvidenceListItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class RunEvidenceAttempt(RunEvidenceSchema):
    attempt_id: UUID
    attempt_no: int = Field(ge=1, le=2)
    status: AttemptStatus
    started_at: datetime
    finished_at: datetime | None
    status_code: int | None = Field(default=None, ge=100, le=599)
    latency_ms: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    error_type: str | None


class RunEvidenceCitation(RunEvidenceSchema):
    citation_id: UUID
    position: int = Field(ge=0, le=2)
    original_url: str
    final_url: str | None
    fetch_status: CitationFetchStatus
    http_status: int | None = Field(default=None, ge=100, le=599)
    authenticity_score: float | None = Field(default=None, ge=0, le=100)
    support_score: float | None = Field(default=None, ge=0, le=100)
    judge_reason: str | None
    content_artifact: RunEvidenceArtifactMetadata | None
    judge_artifact: RunEvidenceArtifactMetadata | None


RunEvidenceCandidateAnswerStatus = Literal[
    "available", "not_recorded", "expired", "unavailable"
]


class RunEvidenceDetail(RunEvidenceListItem):
    prompt: str
    choices: list[RunEvidenceQuestionChoice] | None
    reference_answer: str
    candidate_answer: str | None
    candidate_answer_status: RunEvidenceCandidateAnswerStatus
    attempts: list[RunEvidenceAttempt]
    score: RunEvidenceScoreDetail | None
    citations: list[RunEvidenceCitation]
    response_artifact: RunEvidenceArtifactMetadata | None


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
