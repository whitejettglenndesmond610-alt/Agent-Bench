from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agent_eval.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now
from agent_eval.db.enums import (
    Ability,
    AgentStatus,
    AgentVersionLifecycle,
    ArtifactKind,
    AttemptStatus,
    AuthType,
    CitationFetchStatus,
    DatasetStatus,
    DesiredRunState,
    ExecutionStatus,
    Language,
    NotificationType,
    PreflightStatus,
    QuestionType,
    ResultValidityStatus,
    RunAgentStatus,
    RunScope,
    RunStage,
    RunStatus,
    SessionRole,
)


def enum_column(enum_class: type[StrEnum], *, name: str) -> Enum:
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda values: [item.value for item in values],
        validate_strings=True,
    )


class Agent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    logo_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[AgentStatus] = mapped_column(
        enum_column(AgentStatus, name="agent_status"),
        default=AgentStatus.ENABLED,
        nullable=False,
        index=True,
    )

    versions: Mapped[list["AgentVersion"]] = relationship(back_populates="agent")


class AgentVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_version"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("timeout_seconds BETWEEN 1 AND 600", name="timeout_range"),
        CheckConstraint("concurrency BETWEEN 1 AND 20", name="concurrency_range"),
    )

    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_type: Mapped[AuthType] = mapped_column(
        enum_column(AuthType, name="auth_type"), default=AuthType.NONE, nullable=False
    )
    encrypted_token: Mapped[str | None] = mapped_column(Text)
    request_template: Mapped[str] = mapped_column(Text, nullable=False)
    answer_jsonpath: Mapped[str] = mapped_column(String(500), nullable=False)
    citations_jsonpath: Mapped[str | None] = mapped_column(String(500))
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    concurrency: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    max_prompt_characters: Mapped[int | None] = mapped_column(Integer)
    lifecycle_status: Mapped[AgentVersionLifecycle] = mapped_column(
        Enum(
            AgentVersionLifecycle,
            name="agent_version_lifecycle",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda values: [item.value for item in values],
            validate_strings=True,
        ),
        default=AgentVersionLifecycle.ACTIVE,
        nullable=False,
        index=True,
    )
    preflight_status: Mapped[PreflightStatus] = mapped_column(
        Enum(
            PreflightStatus,
            name="preflight_status",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda values: [item.value for item in values],
            validate_strings=True,
        ),
        default=PreflightStatus.NOT_RUN,
        nullable=False,
    )
    preflight_report_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    preflight_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    agent: Mapped[Agent] = relationship(back_populates="versions")
    run_agents: Mapped[list["RunAgent"]] = relationship(back_populates="agent_version")


class Benchmark(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "benchmarks"
    __table_args__ = (UniqueConstraint("name", "language", name="uq_benchmarks_name_language"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    ability: Mapped[Ability] = mapped_column(
        enum_column(Ability, name="ability"), nullable=False, index=True
    )
    language: Mapped[Language] = mapped_column(
        enum_column(Language, name="language"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    license_note: Mapped[str | None] = mapped_column(Text)

    versions: Mapped[list["DatasetVersion"]] = relationship(back_populates="benchmark")


class DatasetVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("benchmark_id", "version", name="uq_dataset_versions_version"),
        UniqueConstraint("benchmark_id", "content_hash", name="uq_dataset_versions_hash"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("question_count >= 0", name="question_count_nonnegative"),
        CheckConstraint("length(cohort_key) > 0", name="cohort_key_nonempty"),
    )

    benchmark_id: Mapped[UUID] = mapped_column(
        ForeignKey("benchmarks.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_version: Mapped[str] = mapped_column(String(120), nullable=False)
    sampling_seed: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cohort_key: Mapped[str] = mapped_column(
        String(80), default="legacy-v1", server_default="legacy-v1", nullable=False, index=True
    )
    status: Mapped[DatasetStatus] = mapped_column(
        enum_column(DatasetStatus, name="dataset_status"),
        default=DatasetStatus.DRAFT,
        nullable=False,
        index=True,
    )
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    benchmark: Mapped[Benchmark] = relationship(back_populates="versions")
    questions: Mapped[list["Question"]] = relationship(back_populates="dataset_version")
    run_links: Mapped[list["RunDatasetVersion"]] = relationship(back_populates="dataset_version")


class Question(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "questions"
    __table_args__ = (
        UniqueConstraint("dataset_version_id", "external_id", name="uq_questions_dataset_external"),
    )

    dataset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[QuestionType] = mapped_column(
        enum_column(QuestionType, name="question_type"), nullable=False, index=True
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    choices_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    reference_answer: Mapped[str] = mapped_column(Text, nullable=False)
    requires_citations: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    difficulty: Mapped[str | None] = mapped_column(String(80))
    domain: Mapped[str | None] = mapped_column(String(120))

    dataset_version: Mapped[DatasetVersion] = relationship(back_populates="questions")
    execution_items: Mapped[list["ExecutionItem"]] = relationship(back_populates="question")


class JudgeConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "judge_configs"
    __table_args__ = (
        UniqueConstraint("provider", "model", "prompt_version", name="uq_judge_configs_identity"),
    )

    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    runs: Mapped[list["EvaluationRun"]] = relationship(back_populates="judge_config")


class ScoreConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "score_configs"
    __table_args__ = (
        CheckConstraint(
            "reasoning_weight + retrieval_weight + data_weight + "
            "instruction_following_weight + long_context_weight = 100",
            name="ability_weights_total",
        ),
        CheckConstraint(
            "retrieval_answer_weight + retrieval_citation_weight = 100",
            name="retrieval_weights_total",
        ),
        CheckConstraint(
            "citation_authenticity_weight + citation_support_weight = 100",
            name="citation_weights_total",
        ),
        CheckConstraint(
            "reasoning_weight BETWEEN 0 AND 100 AND "
            "retrieval_weight BETWEEN 0 AND 100 AND data_weight BETWEEN 0 AND 100 AND "
            "instruction_following_weight BETWEEN 0 AND 100 AND "
            "long_context_weight BETWEEN 0 AND 100",
            name="ability_weights_range",
        ),
        CheckConstraint(
            "retrieval_answer_weight BETWEEN 0 AND 100 AND "
            "retrieval_citation_weight BETWEEN 0 AND 100",
            name="retrieval_weights_range",
        ),
        CheckConstraint(
            "citation_authenticity_weight BETWEEN 0 AND 100 AND "
            "citation_support_weight BETWEEN 0 AND 100",
            name="citation_weights_range",
        ),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    reasoning_weight: Mapped[int] = mapped_column(SmallInteger, default=40, nullable=False)
    retrieval_weight: Mapped[int] = mapped_column(SmallInteger, default=35, nullable=False)
    data_weight: Mapped[int] = mapped_column(SmallInteger, default=25, nullable=False)
    instruction_following_weight: Mapped[int] = mapped_column(
        SmallInteger, default=0, server_default=text("0"), nullable=False
    )
    long_context_weight: Mapped[int] = mapped_column(
        SmallInteger, default=0, server_default=text("0"), nullable=False
    )
    retrieval_answer_weight: Mapped[int] = mapped_column(SmallInteger, default=70, nullable=False)
    retrieval_citation_weight: Mapped[int] = mapped_column(SmallInteger, default=30, nullable=False)
    citation_authenticity_weight: Mapped[int] = mapped_column(
        SmallInteger, default=50, nullable=False
    )
    citation_support_weight: Mapped[int] = mapped_column(SmallInteger, default=50, nullable=False)

    runs: Mapped[list["EvaluationRun"]] = relationship(back_populates="score_config")


class EvaluationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint(
            "(status IN ('running', 'paused', 'scoring') AND active_slot BETWEEN 1 AND 3 "
            "AND claim_owner_id IS NOT NULL AND claimed_at IS NOT NULL) OR "
            "(status NOT IN ('running', 'paused', 'scoring') AND active_slot IS NULL "
            "AND claim_owner_id IS NULL AND claimed_at IS NULL)",
            name="active_slot_matches_status",
        ),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus, name="run_status"),
        default=RunStatus.DRAFT,
        nullable=False,
        index=True,
    )
    desired_state: Mapped[DesiredRunState] = mapped_column(
        enum_column(DesiredRunState, name="desired_run_state"),
        default=DesiredRunState.RUNNING,
        nullable=False,
    )
    stage: Mapped[RunStage] = mapped_column(
        enum_column(RunStage, name="run_stage"),
        default=RunStage.PREPARING,
        nullable=False,
    )
    active_slot: Mapped[int | None] = mapped_column(SmallInteger, unique=True)
    claim_owner_id: Mapped[str | None] = mapped_column(String(120), index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    dataset_bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_key: Mapped[str] = mapped_column(
        String(80), default="comprehensive", server_default="comprehensive", nullable=False
    )
    run_scope: Mapped[RunScope] = mapped_column(
        enum_column(RunScope, name="run_scope"),
        default=RunScope.COMPREHENSIVE,
        server_default=RunScope.COMPREHENSIVE.value,
        nullable=False,
    )
    target_ability: Mapped[Ability | None] = mapped_column(
        enum_column(Ability, name="run_target_ability")
    )
    ranking_eligible: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    protocol_hash: Mapped[str] = mapped_column(
        String(64), default=lambda: "0" * 64, server_default="0" * 64, nullable=False
    )
    judge_config_id: Mapped[UUID] = mapped_column(
        ForeignKey("judge_configs.id", ondelete="RESTRICT"), nullable=False
    )
    score_config_id: Mapped[UUID] = mapped_column(
        ForeignKey("score_configs.id", ondelete="RESTRICT"), nullable=False
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    system_error: Mapped[str | None] = mapped_column(Text)

    judge_config: Mapped[JudgeConfig] = relationship(back_populates="runs")
    score_config: Mapped[ScoreConfig] = relationship(back_populates="runs")
    dataset_links: Mapped[list["RunDatasetVersion"]] = relationship(back_populates="run")
    run_agents: Mapped[list["RunAgent"]] = relationship(back_populates="run")
    snapshots: Mapped[list["LeaderboardSnapshot"]] = relationship(back_populates="run")


class RunDatasetVersion(Base):
    __tablename__ = "run_dataset_versions"

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"), primary_key=True
    )
    dataset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), primary_key=True
    )

    run: Mapped[EvaluationRun] = relationship(back_populates="dataset_links")
    dataset_version: Mapped[DatasetVersion] = relationship(back_populates="run_links")


class RunAgent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "run_agents"
    __table_args__ = (
        UniqueConstraint("run_id", "agent_version_id", name="uq_run_agents_run_agent"),
        CheckConstraint(
            "total >= 0 AND completed >= 0 AND failed >= 0 AND retried >= 0",
            name="counts_nonnegative",
        ),
        CheckConstraint("completed + failed <= total", name="counts_within_total"),
        CheckConstraint(
            "configuration_failure_count >= 0",
            name="configuration_failure_count_nonnegative",
        ),
        CheckConstraint(
            "configuration_failure_rate BETWEEN 0 AND 100",
            name="configuration_failure_rate_range",
        ),
        CheckConstraint(
            "validity_status IN ('pending', 'valid', 'invalid')",
            name="validity_status",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[RunAgentStatus] = mapped_column(
        enum_column(RunAgentStatus, name="run_agent_status"),
        default=RunAgentStatus.QUEUED,
        nullable=False,
    )
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retried: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    validity_status: Mapped[ResultValidityStatus] = mapped_column(
        Enum(
            ResultValidityStatus,
            name="result_validity_status",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda values: [item.value for item in values],
            validate_strings=True,
        ),
        default=ResultValidityStatus.PENDING,
        server_default=ResultValidityStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    validity_reason: Mapped[str | None] = mapped_column(String(160))
    configuration_failure_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    configuration_failure_rate: Mapped[Decimal] = mapped_column(
        Numeric(7, 4), default=Decimal(0), server_default=text("0"), nullable=False
    )

    run: Mapped[EvaluationRun] = relationship(back_populates="run_agents")
    agent_version: Mapped[AgentVersion] = relationship(back_populates="run_agents")
    execution_items: Mapped[list["ExecutionItem"]] = relationship(back_populates="run_agent")


class ArtifactIndex(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "artifact_index"
    __table_args__ = (CheckConstraint("size_bytes >= 0", name="size_nonnegative"),)

    kind: Mapped[ArtifactKind] = mapped_column(
        enum_column(ArtifactKind, name="artifact_kind"), nullable=False, index=True
    )
    relative_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ExecutionItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "execution_items"
    __table_args__ = (
        UniqueConstraint("run_agent_id", "question_id", name="uq_execution_items_agent_question"),
        CheckConstraint("attempt_count BETWEEN 0 AND 2", name="attempt_count_range"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_nonnegative"),
    )

    run_agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[ExecutionStatus] = mapped_column(
        enum_column(ExecutionStatus, name="execution_status"),
        default=ExecutionStatus.PENDING,
        nullable=False,
        index=True,
    )
    answer_preview: Mapped[str | None] = mapped_column(String(500))
    answer_hash: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("artifact_index.id", ondelete="SET NULL")
    )
    error_type: Mapped[str | None] = mapped_column(String(80), index=True)

    run_agent: Mapped[RunAgent] = relationship(back_populates="execution_items")
    question: Mapped[Question] = relationship(back_populates="execution_items")
    artifact: Mapped[ArtifactIndex | None] = relationship(foreign_keys=[artifact_id])
    attempts: Mapped[list["HttpAttempt"]] = relationship(back_populates="execution_item")
    citations: Mapped[list["CitationRecord"]] = relationship(back_populates="execution_item")
    score: Mapped["ScoreRecord | None"] = relationship(back_populates="execution_item")


class HttpAttempt(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "http_attempts"
    __table_args__ = (
        UniqueConstraint("execution_item_id", "attempt_no", name="uq_http_attempts_item_attempt"),
        CheckConstraint("attempt_no BETWEEN 1 AND 2", name="attempt_no_range"),
        CheckConstraint(
            "status_code IS NULL OR status_code BETWEEN 100 AND 599",
            name="status_code_range",
        ),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_nonnegative"),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0", name="input_tokens_nonnegative"
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0", name="output_tokens_nonnegative"
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0", name="total_tokens_nonnegative"
        ),
    )

    execution_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[AttemptStatus] = mapped_column(
        enum_column(AttemptStatus, name="attempt_status"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_code: Mapped[int | None] = mapped_column(SmallInteger)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(80))

    execution_item: Mapped[ExecutionItem] = relationship(back_populates="attempts")


class CitationRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "citation_records"
    __table_args__ = (
        UniqueConstraint("execution_item_id", "position", name="uq_citation_records_item_position"),
        CheckConstraint("position BETWEEN 0 AND 2", name="position_range"),
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="http_status_range",
        ),
        CheckConstraint(
            "authenticity_score IS NULL OR authenticity_score BETWEEN 0 AND 100",
            name="authenticity_score_range",
        ),
        CheckConstraint(
            "support_score IS NULL OR support_score BETWEEN 0 AND 100",
            name="support_score_range",
        ),
    )

    execution_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    fetch_status: Mapped[CitationFetchStatus] = mapped_column(
        enum_column(CitationFetchStatus, name="citation_fetch_status"),
        default=CitationFetchStatus.PENDING,
        nullable=False,
        index=True,
    )
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    authenticity_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    support_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    judge_reason: Mapped[str | None] = mapped_column(String(500))
    artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("artifact_index.id", ondelete="SET NULL")
    )
    judge_artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("artifact_index.id", ondelete="SET NULL")
    )

    execution_item: Mapped[ExecutionItem] = relationship(back_populates="citations")
    artifact: Mapped[ArtifactIndex | None] = relationship(foreign_keys=[artifact_id])
    judge_artifact: Mapped[ArtifactIndex | None] = relationship(foreign_keys=[judge_artifact_id])


class ScoreRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "score_records"
    __table_args__ = (
        CheckConstraint("answer_score BETWEEN 0 AND 100", name="answer_score_range"),
        CheckConstraint(
            "citation_score IS NULL OR citation_score BETWEEN 0 AND 100",
            name="citation_score_range",
        ),
        CheckConstraint("final_score BETWEEN 0 AND 100", name="final_score_range"),
    )

    execution_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_items.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    answer_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    citation_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    final_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    judge_reason: Mapped[str | None] = mapped_column(String(500))
    judge_artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("artifact_index.id", ondelete="SET NULL")
    )

    execution_item: Mapped[ExecutionItem] = relationship(back_populates="score")
    judge_artifact: Mapped[ArtifactIndex | None] = relationship(foreign_keys=[judge_artifact_id])


class LeaderboardSnapshot(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "leaderboard_snapshots"
    __table_args__ = (
        Index(
            "uq_leaderboard_snapshots_current",
            "is_current",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    rankings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    run: Mapped[EvaluationRun] = relationship(back_populates="snapshots")
    results: Mapped[list["LeaderboardResult"]] = relationship(back_populates="snapshot")


class LeaderboardResult(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "leaderboard_results"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "agent_version_id",
            "metric",
            name="uq_leaderboard_results_snapshot_agent_version_metric",
        ),
        Index("ix_leaderboard_results_metric_protocol_hash", "metric", "protocol_hash"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("leaderboard_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    run_agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_agents.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    agent_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(50), nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    protocol_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    benchmark_count: Mapped[int] = mapped_column(Integer, nullable=False)

    snapshot: Mapped[LeaderboardSnapshot] = relationship(back_populates="results")


class Session(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "sessions"

    role: Mapped[SessionRole] = mapped_column(
        enum_column(SessionRole, name="session_role"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Notification(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "notifications"

    type: Mapped[NotificationType] = mapped_column(
        enum_column(NotificationType, name="notification_type"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerLease(Base):
    __tablename__ = "worker_leases"

    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(120), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResourceLease(Base):
    __tablename__ = "resource_leases"
    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "resource_key",
            "slot_number",
            name="uq_resource_leases_resource_slot",
        ),
        CheckConstraint("slot_number >= 1", name="slot_number_positive"),
        Index(
            "ix_resource_leases_resource_lookup",
            "resource_type",
            "resource_key",
            "lease_expires_at",
        ),
    )

    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(255), nullable=False)
    slot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    holder_token: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
