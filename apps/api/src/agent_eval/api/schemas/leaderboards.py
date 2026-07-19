from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_eval.db.enums import Ability, Language, ResultValidityStatus


class BenchmarkRanking(BaseModel):
    benchmark_id: UUID
    name: str
    ability: Ability
    language: Language
    score: float


class AgentRanking(BaseModel):
    rank: int
    run_agent_id: UUID
    agent_id: UUID
    agent_version_id: UUID
    agent_name: str
    provider: str
    version: int
    overall_score: float
    ability_scores: dict[Ability, float]
    language_scores: dict[Language, float]
    benchmark_scores: list[BenchmarkRanking]
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


class LeaderboardSnapshotResponse(BaseModel):
    id: UUID
    run_id: UUID
    run_name: str
    dataset_bundle_hash: str
    published_at: datetime
    rankings: list[AgentRanking]


class LeaderboardHistoryItem(BaseModel):
    id: UUID
    run_id: UUID
    run_name: str
    published_at: datetime
    agent_count: int
    is_current: bool


LeaderboardMetric = Literal[
    "overall",
    "reasoning",
    "retrieval",
    "data_reasoning",
    "instruction_following",
    "long_context",
]
GlobalEvaluationStatus = Literal[
    "not_evaluated", "queued", "running", "evaluated", "needs_retest", "failed", "invalid"
]


class GlobalRankedAgent(BaseModel):
    rank: int
    score: float
    agent_id: UUID
    agent_version_id: UUID
    agent_name: str
    provider: str
    version: int
    status: Literal["current", "needs_retest"]
    source_snapshot_id: UUID
    source_run_id: UUID
    source_run_name: str
    source_profile_key: str
    source_published_at: datetime
    benchmark_count: int
    success_rate: float
    anomaly_rate: float
    http_call_count: int
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    total_tokens: int | None = None
    token_usage_available_count: int = 0
    token_usage_missing_count: int = 0
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    latency_sample_count: int = 0
    benchmark_scores: list[BenchmarkRanking]


class GlobalUnrankedAgent(BaseModel):
    agent_id: UUID
    agent_version_id: UUID | None
    agent_name: str
    provider: str
    version: int | None
    status: Literal["not_evaluated"] = "not_evaluated"


class GlobalLeaderboardResponse(BaseModel):
    metric: LeaderboardMetric
    label: str
    protocol_hash: str | None
    generated_at: datetime
    protocol_published_at: datetime | None
    eligible_agent_count: int
    benchmark_count: int
    question_count: int
    ranked_agents: list[GlobalRankedAgent]
    unranked_agents: list[GlobalUnrankedAgent]


class GlobalMetricProtocol(BaseModel):
    metric: LeaderboardMetric
    label: str
    protocol_hash: str | None
    published_at: datetime | None
    benchmark_count: int
    question_count: int
    ranked_agent_count: int


class GlobalOverviewMetric(BaseModel):
    status: GlobalEvaluationStatus
    message: str | None = None
    rank: int | None = None
    score: float | None = None
    source_agent_version_id: UUID | None = None
    source_version: int | None = None
    source_snapshot_id: UUID | None = None
    source_run_id: UUID | None = None
    source_run_name: str | None = None
    source_profile_key: str | None = None
    source_published_at: datetime | None = None
    benchmark_count: int = 0
    success_rate: float | None = None
    anomaly_rate: float | None = None
    http_call_count: int | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    total_tokens: int | None = None
    token_usage_available_count: int = 0
    token_usage_missing_count: int = 0
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    latency_sample_count: int = 0
    benchmark_scores: list[BenchmarkRanking] = Field(default_factory=list)


class GlobalOverviewAgent(BaseModel):
    agent_id: UUID
    agent_version_id: UUID | None
    agent_name: str
    provider: str
    version: int | None
    metrics: dict[LeaderboardMetric, GlobalOverviewMetric]


class GlobalLeaderboardOverviewResponse(BaseModel):
    generated_at: datetime
    eligible_agent_count: int
    protocols: list[GlobalMetricProtocol]
    agents: list[GlobalOverviewAgent]


class AgentScoreHistoryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    score: float = Field(ge=0, le=100)
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_matches_current: bool
    is_current_source: bool
    agent_version_id: UUID
    version: int = Field(ge=1)
    source_snapshot_id: UUID
    source_run_id: UUID
    source_run_name: str
    source_profile_key: str
    source_published_at: datetime
    benchmark_count: int = Field(ge=0)


class AgentLeaderboardDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agent_id: UUID
    agent_version_id: UUID | None
    agent_name: str
    provider: str
    description: str | None
    logo_url: str | None
    version: int | None = Field(default=None, ge=1)
    metrics: dict[LeaderboardMetric, GlobalOverviewMetric]
    score_history: dict[LeaderboardMetric, list[AgentScoreHistoryPoint]]
