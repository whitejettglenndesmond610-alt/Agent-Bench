from enum import StrEnum


class AgentStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class AuthType(StrEnum):
    NONE = "none"
    BEARER = "bearer"


class AgentVersionLifecycle(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"
    ACTIVE = "active"
    RETIRED = "retired"


class PreflightStatus(StrEnum):
    NOT_RUN = "not_run"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class Ability(StrEnum):
    REASONING = "reasoning"
    RETRIEVAL = "retrieval"
    DATA_REASONING = "data_reasoning"
    INSTRUCTION_FOLLOWING = "instruction_following"
    LONG_CONTEXT = "long_context"


RELEASED_ABILITIES = frozenset(
    {Ability.REASONING, Ability.RETRIEVAL, Ability.DATA_REASONING}
)


class Language(StrEnum):
    ZH = "zh"
    EN = "en"


class DatasetStatus(StrEnum):
    DRAFT = "draft"
    FROZEN = "frozen"


class QuestionType(StrEnum):
    MULTIPLE_CHOICE = "multiple_choice"
    SHORT_ANSWER = "short_answer"
    OPEN_ENDED = "open_ended"


class RunStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SCORING = "scoring"
    COMPLETED = "completed"
    PUBLISHED = "published"
    SYSTEM_FAILED = "system_failed"
    CANCELED = "canceled"


class RunScope(StrEnum):
    COMPREHENSIVE = "comprehensive"
    SPECIALIST = "specialist"
    SMOKE = "smoke"


class DesiredRunState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    CANCELED = "canceled"


class RunStage(StrEnum):
    PREPARING = "preparing"
    INFERENCE = "inference"
    CITATION_FETCH = "citation_fetch"
    JUDGING = "judging"
    AGGREGATING = "aggregating"
    READY_TO_PUBLISH = "ready_to_publish"
    PUBLISHING = "publishing"


class RunAgentStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELED = "canceled"


class ResultValidityStatus(StrEnum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    ANSWERED = "answered"
    FAILED = "failed"
    SCORED = "scored"
    CANCELED = "canceled"


class AttemptStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


class CitationFetchStatus(StrEnum):
    PENDING = "pending"
    FETCHING = "fetching"
    FETCHED = "fetched"
    BLOCKED = "blocked"
    UNREACHABLE = "unreachable"
    UNSUPPORTED = "unsupported"


class ArtifactKind(StrEnum):
    AGENT_RESPONSE = "agent_response"
    CITATION_CONTENT = "citation_content"
    JUDGE_RESPONSE = "judge_response"


class SessionRole(StrEnum):
    READ_ONLY = "read_only"
    OPERATOR = "operator"


class AuthSource(StrEnum):
    LEGACY_SHARED = "legacy_shared"
    LOCAL = "local"


class WorkspaceMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class WorkspaceAttemptStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class EvaluationCaseStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    PUBLISHED = "published"


class NotificationType(StrEnum):
    RUN_PUBLISHED = "run_published"
    RUN_FAILED = "run_failed"
