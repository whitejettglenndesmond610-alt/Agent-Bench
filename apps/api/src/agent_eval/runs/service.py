import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from agent_eval.datasets.importer import BALANCED_V2_COHORT_KEY, LEGACY_COHORT_KEY
from agent_eval.db.enums import (
    Ability,
    AgentStatus,
    AgentVersionLifecycle,
    DatasetStatus,
    RunScope,
    RunStatus,
)
from agent_eval.db.models import (
    AgentVersion,
    DatasetVersion,
    EvaluationRun,
    ExecutionItem,
    JudgeConfig,
    LeaderboardResult,
    Question,
    RunAgent,
    RunDatasetVersion,
    ScoreConfig,
)
from agent_eval.runs.compatibility import analyze_profile_compatibility
from agent_eval.runs.profiles import (
    BALANCED_V2_PROFILE_KEY,
    HELDOUT_V1_PROFILE_KEY,
    MCP_PROFILE_KEYS,
    MCP_PROFILES,
    OFFICIAL_V4_PROFILE_KEY,
    TAU2_PROFILE_KEYS,
    TAU2_PROFILES,
    UNIFIED_V3_PROFILE_KEY,
    UNIFIED_V3_PROFILE_KEYS,
    build_run_profiles,
    is_complete_balanced_v2,
    is_complete_heldout_v1,
    is_complete_official_v4,
    is_complete_tau2_domain,
    is_complete_unified_v3,
)
from agent_eval.scoring.aggregation import AGGREGATION_VERSION
from agent_eval.scoring.configs import (
    BALANCED_V2_ABILITY_WEIGHTS,
    LEGACY_ABILITY_WEIGHTS,
    has_ability_weights,
)

# Interactive task profiles (tau2 / MCPMark) lock their dataset bundle and
# never publish to the leaderboards.
INTERACTIVE_PROFILE_KEYS = TAU2_PROFILE_KEYS | MCP_PROFILE_KEYS
INTERACTIVE_PROFILES = {**TAU2_PROFILES, **MCP_PROFILES}


class RunConfigurationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedRun:
    run: EvaluationRun
    agent_version_ids: list[UUID]
    dataset_version_ids: list[UUID]
    question_count: int
    execution_item_count: int


def dataset_bundle_hash(versions: list[DatasetVersion]) -> str:
    digest = hashlib.sha256()
    for version in sorted(versions, key=lambda item: str(item.id)):
        digest.update(f"{version.id}:{version.content_hash}\n".encode())
    return digest.hexdigest()


def run_protocol_hash(
    *,
    profile_key: str,
    run_scope: RunScope,
    target_ability: Ability | None,
    dataset_versions: list[DatasetVersion],
    judge_config: JudgeConfig,
    score_config: ScoreConfig | None,
) -> str:
    protocol_epoch = _protocol_epoch(profile_key, dataset_versions)
    protocol: dict[str, Any] = {
        "aggregation_version": AGGREGATION_VERSION,
        "datasets": [
            {"content_hash": version.content_hash, "id": str(version.id)}
            for version in sorted(dataset_versions, key=lambda item: str(item.id))
        ],
        "judge": {
            "model": judge_config.model,
            "prompt_version": judge_config.prompt_version,
            "provider": judge_config.provider,
        },
        "profile": {
            "key": profile_key,
            "scope": run_scope.value,
            "target_ability": target_ability.value if target_ability is not None else None,
        },
        "score": (
            {
                "citation_authenticity_weight": score_config.citation_authenticity_weight,
                "citation_support_weight": score_config.citation_support_weight,
                "data_weight": score_config.data_weight,
                "reasoning_weight": score_config.reasoning_weight,
                "retrieval_answer_weight": score_config.retrieval_answer_weight,
                "retrieval_citation_weight": score_config.retrieval_citation_weight,
                "retrieval_weight": score_config.retrieval_weight,
            }
            if score_config is not None
            else None
        ),
    }
    if protocol_epoch is not None:
        protocol["protocol_epoch"] = protocol_epoch
        for dataset in protocol["datasets"]:
            version_id = UUID(dataset["id"])
            dataset["cohort_key"] = next(
                version.cohort_key for version in dataset_versions if version.id == version_id
            )
        if score_config is not None:
            protocol_score = protocol["score"]
            assert isinstance(protocol_score, dict)
            protocol_score["instruction_following_weight"] = (
                score_config.instruction_following_weight
            )
            protocol_score["long_context_weight"] = score_config.long_context_weight
    serialized = json.dumps(protocol, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def metric_protocol_hash(
    *,
    metric: str,
    dataset_versions: list[DatasetVersion],
    judge_config: JudgeConfig,
    score_config: ScoreConfig,
    profile_key: str | None = None,
) -> str:
    relevant_versions = [
        version
        for version in dataset_versions
        if metric == "overall" or version.benchmark.ability.value == metric
    ]
    protocol: dict[str, Any] = {
        "aggregation_version": AGGREGATION_VERSION,
        "metric": metric,
        "datasets": [
            {"content_hash": version.content_hash, "id": str(version.id)}
            for version in sorted(relevant_versions, key=lambda item: str(item.id))
        ],
        "judge": {
            "model": judge_config.model,
            "prompt_version": judge_config.prompt_version,
            "provider": judge_config.provider,
        },
        "score": {
            "citation_authenticity_weight": score_config.citation_authenticity_weight,
            "citation_support_weight": score_config.citation_support_weight,
            "data_weight": score_config.data_weight,
            "reasoning_weight": score_config.reasoning_weight,
            "retrieval_answer_weight": score_config.retrieval_answer_weight,
            "retrieval_citation_weight": score_config.retrieval_citation_weight,
            "retrieval_weight": score_config.retrieval_weight,
        },
    }
    protocol_epoch = _protocol_epoch(profile_key, relevant_versions)
    if protocol_epoch is not None:
        protocol["protocol_epoch"] = protocol_epoch
        for dataset in protocol["datasets"]:
            version_id = UUID(dataset["id"])
            dataset["cohort_key"] = next(
                version.cohort_key for version in relevant_versions if version.id == version_id
            )
        protocol["score"]["instruction_following_weight"] = (
            score_config.instruction_following_weight
        )
        protocol["score"]["long_context_weight"] = score_config.long_context_weight
    serialized = json.dumps(protocol, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _is_balanced_v2(dataset_versions: list[DatasetVersion]) -> bool:
    return bool(dataset_versions) and all(
        version.cohort_key == BALANCED_V2_COHORT_KEY for version in dataset_versions
    )


def _protocol_epoch(
    profile_key: str | None, dataset_versions: list[DatasetVersion]
) -> str | None:
    if profile_key in UNIFIED_V3_PROFILE_KEYS:
        return UNIFIED_V3_PROFILE_KEY
    if profile_key == OFFICIAL_V4_PROFILE_KEY:
        return OFFICIAL_V4_PROFILE_KEY
    if _is_balanced_v2(dataset_versions):
        return BALANCED_V2_PROFILE_KEY
    return None


async def prepare_evaluation_run(
    session: AsyncSession,
    *,
    name: str,
    agent_version_ids: list[UUID],
    dataset_version_ids: list[UUID],
    judge_config_id: UUID,
    score_config_id: UUID | None,
    profile_key: str = "comprehensive",
    run_scope: RunScope = RunScope.COMPREHENSIVE,
    target_ability: Ability | None = None,
    ranking_eligible: bool = True,
    trial_count: int = 1,
    prevent_duplicates: bool = False,
    force_retest: bool = False,
) -> PreparedRun:
    if not 1 <= trial_count <= 10:
        raise RunConfigurationError("invalid_trial_count", "每题运行次数必须在 1 到 10 之间")
    agent_versions = list(
        await session.scalars(
            select(AgentVersion)
            .options(joinedload(AgentVersion.agent))
            .where(AgentVersion.id.in_(agent_version_ids))
        )
    )
    if {version.id for version in agent_versions} != set(agent_version_ids):
        raise RunConfigurationError("agent_version_not_found", "一个或多个 Agent 配置版本不存在")
    if any(version.agent.owner_user_id is not None for version in agent_versions):
        raise RunConfigurationError(
            "private_agent_not_allowed", "私有 Agent 不能用于正式评测"
        )
    invalid_lifecycle = [
        version
        for version in agent_versions
        if version.lifecycle_status != AgentVersionLifecycle.ACTIVE
        and not (
            version.lifecycle_status == AgentVersionLifecycle.VERIFIED
            and run_scope == RunScope.SMOKE
            and not ranking_eligible
        )
    ]
    if invalid_lifecycle:
        if any(
            version.lifecycle_status == AgentVersionLifecycle.VERIFIED
            for version in invalid_lifecycle
        ):
            raise RunConfigurationError(
                "candidate_smoke_only", "候选版本只能运行不进入排行榜的 Smoke Profile"
            )
        raise RunConfigurationError(
            "agent_version_not_active", "只能使用已设为当前的 Agent 配置版本"
        )
    if any(
        not version.enabled or version.agent.status != AgentStatus.ENABLED
        for version in agent_versions
    ):
        raise RunConfigurationError("agent_version_disabled", "只能选择已启用的 Agent 配置版本")
    if len({version.agent_id for version in agent_versions}) != len(agent_versions):
        raise RunConfigurationError(
            "duplicate_logical_agent", "同一评测任务不能选择同一 Agent 的多个配置版本"
        )

    dataset_versions = list(
        await session.scalars(
            select(DatasetVersion)
            .options(joinedload(DatasetVersion.benchmark))
            .where(DatasetVersion.id.in_(dataset_version_ids))
        )
    )
    if {version.id for version in dataset_versions} != set(dataset_version_ids):
        raise RunConfigurationError("dataset_version_not_found", "一个或多个题库版本不存在")
    if any(version.status != DatasetStatus.FROZEN for version in dataset_versions):
        raise RunConfigurationError("dataset_not_frozen", "只能使用已冻结的题库版本")
    if any(not version.run_eligible for version in dataset_versions):
        raise RunConfigurationError(
            "dataset_not_run_eligible", "精选发布数据集尚未获准进入评测任务"
        )
    cohort_keys = {version.cohort_key for version in dataset_versions}
    unified_profile = profile_key in UNIFIED_V3_PROFILE_KEYS
    if profile_key.startswith(UNIFIED_V3_PROFILE_KEY) and not unified_profile:
        raise RunConfigurationError("profile_not_found", "unified-v3 评测配置不存在")
    locked_profile = unified_profile or profile_key in {
        OFFICIAL_V4_PROFILE_KEY,
        HELDOUT_V1_PROFILE_KEY,
        *INTERACTIVE_PROFILE_KEYS,
    }
    if locked_profile:
        profile = next(
            (
                item
                for item in await build_run_profiles(session)
                if item.key == profile_key
            ),
            None,
        )
        if (
            profile is None
            or set(profile.dataset_version_ids) != set(dataset_version_ids)
            or profile.scope != run_scope
            or profile.target_ability != target_ability
            or (not ranking_eligible and profile_key not in INTERACTIVE_PROFILE_KEYS)
            or (profile_key in INTERACTIVE_PROFILE_KEYS and ranking_eligible)
        ):
            raise RunConfigurationError(
                (
                    "unified_v3_incomplete"
                    if unified_profile
                    else (
                        "official_v4_incomplete"
                        if profile_key == OFFICIAL_V4_PROFILE_KEY
                        else (
                            "heldout_v1_incomplete"
                            if profile_key == HELDOUT_V1_PROFILE_KEY
                            else f"{profile_key.replace('-', '_')}_incomplete"
                        )
                    )
                ),
                "必须使用完整且经过验证的精确评测配置",
            )
    else:
        expected_cohort = (
            BALANCED_V2_COHORT_KEY
            if profile_key == BALANCED_V2_PROFILE_KEY
            else LEGACY_COHORT_KEY
        )
        if cohort_keys != {expected_cohort}:
            raise RunConfigurationError(
                "profile_cohort_mismatch", "题库 cohort 必须与所选评测配置完全一致"
            )

    judge_config = await session.get(JudgeConfig, judge_config_id)
    if judge_config is None or not judge_config.enabled:
        raise RunConfigurationError("judge_unavailable", "裁判模型配置不存在或未启用")
    score_config: ScoreConfig | None = None
    if score_config_id is not None:
        score_config = await session.get(ScoreConfig, score_config_id)
        if score_config is None:
            raise RunConfigurationError("score_config_not_found", "评分配置不存在")
        expected_weights = (
            BALANCED_V2_ABILITY_WEIGHTS
            if unified_profile or profile_key == BALANCED_V2_PROFILE_KEY
            else LEGACY_ABILITY_WEIGHTS
        )
        if not has_ability_weights(score_config, expected_weights):
            raise RunConfigurationError(
                "profile_score_config_mismatch", "评分配置权重与所选评测配置不匹配"
            )

    questions = list(
        await session.scalars(
            select(Question)
            .options(
                joinedload(Question.dataset_version).joinedload(DatasetVersion.benchmark)
            )
            .where(Question.dataset_version_id.in_(dataset_version_ids))
        )
    )
    counts = Counter(question.dataset_version_id for question in questions)
    if any(counts[version.id] != version.question_count for version in dataset_versions):
        raise RunConfigurationError(
            "dataset_question_count_mismatch", "题库版本题量与实际题目不一致"
        )
    if not questions:
        raise RunConfigurationError("dataset_empty", "所选题库没有题目")
    if profile_key == BALANCED_V2_PROFILE_KEY and not is_complete_balanced_v2(
        dataset_versions, questions
    ):
        raise RunConfigurationError(
            "balanced_v2_incomplete", "balanced-v2 必须使用完整且分布正确的 150 题 cohort"
        )
    if profile_key == UNIFIED_V3_PROFILE_KEY and not is_complete_unified_v3(
        dataset_versions, questions
    ):
        raise RunConfigurationError(
            "unified_v3_incomplete", "unified-v3 必须使用完整且分布正确的 175 题配置"
        )
    if profile_key == OFFICIAL_V4_PROFILE_KEY and not is_complete_official_v4(
        dataset_versions
    ):
        raise RunConfigurationError(
            "official_v4_incomplete", "official-v4 必须使用完整的 100 题权威配置"
        )
    if profile_key == HELDOUT_V1_PROFILE_KEY and not is_complete_heldout_v1(
        dataset_versions
    ):
        raise RunConfigurationError(
            "heldout_v1_incomplete", "heldout-v1 必须使用完整的 100 题 LiveBench+HLE 私有配置"
        )
    if profile_key in INTERACTIVE_PROFILE_KEYS:
        benchmark_name, _profile_name, expected = INTERACTIVE_PROFILES[profile_key]
        if not is_complete_tau2_domain(dataset_versions, benchmark_name, expected):
            raise RunConfigurationError(
                f"{profile_key.replace('-', '_')}_incomplete",
                f"{profile_key} 必须使用完整的 {expected} 题 {benchmark_name} 配置",
            )

    for version in agent_versions:
        compatibility = analyze_profile_compatibility(version, profile_key, questions)
        if not compatibility.compatible:
            raise RunConfigurationError(
                "agent_profile_incompatible",
                f"{version.agent.name} 与 {profile_key} Profile 不兼容："
                f"{'；'.join(compatibility.blocking_reasons)}",
            )

    bundle_hash = dataset_bundle_hash(dataset_versions)
    protocol_hash = run_protocol_hash(
        profile_key=profile_key,
        run_scope=run_scope,
        target_ability=target_ability,
        dataset_versions=dataset_versions,
        judge_config=judge_config,
        score_config=score_config,
    )
    if prevent_duplicates:
        result_metric: str | None = None
        if ranking_eligible and run_scope == RunScope.COMPREHENSIVE:
            result_metric = "overall"
        elif (
            ranking_eligible
            and run_scope == RunScope.SPECIALIST
            and target_ability is not None
        ):
            result_metric = target_ability.value
        result_protocol_hash = None
        if result_metric is not None:
            # ranking-eligible runs always carry a score configuration
            assert score_config is not None
            result_protocol_hash = metric_protocol_hash(
                metric=result_metric,
                dataset_versions=dataset_versions,
                judge_config=judge_config,
                score_config=score_config,
                profile_key=profile_key,
            )
        await session.execute(
            select(AgentVersion.id)
            .where(AgentVersion.id.in_(agent_version_ids))
            .order_by(AgentVersion.id)
            .with_for_update()
        )
        await ensure_run_not_duplicate(
            session,
            agent_version_ids=agent_version_ids,
            profile_key=profile_key,
            protocol_hash=protocol_hash,
            result_metric=result_metric,
            result_protocol_hash=result_protocol_hash,
            force_retest=force_retest,
        )
    run = EvaluationRun(
        name=name,
        status=RunStatus.QUEUED,
        dataset_bundle_hash=bundle_hash,
        profile_key=profile_key,
        run_scope=run_scope,
        target_ability=target_ability,
        ranking_eligible=ranking_eligible,
        trial_count=trial_count,
        protocol_hash=protocol_hash,
        judge_config_id=judge_config.id,
        score_config_id=score_config.id if score_config is not None else None,
    )
    session.add(run)
    await session.flush()
    session.add_all(
        [
            RunDatasetVersion(run_id=run.id, dataset_version_id=version.id)
            for version in dataset_versions
        ]
    )

    run_agents = [
        RunAgent(run_id=run.id, agent_version_id=version.id, total=len(questions) * trial_count)
        for version in agent_versions
    ]
    session.add_all(run_agents)
    await session.flush()
    session.add_all(
        [
            ExecutionItem(
                run_agent_id=run_agent.id,
                question_id=question.id,
                trial_index=trial_index,
            )
            for run_agent in run_agents
            for question in questions
            for trial_index in range(trial_count)
        ]
    )
    await session.flush()

    return PreparedRun(
        run=run,
        agent_version_ids=sorted(agent_version_ids, key=str),
        dataset_version_ids=sorted(dataset_version_ids, key=str),
        question_count=len(questions),
        execution_item_count=len(questions) * len(agent_versions) * trial_count,
    )


async def ensure_run_not_duplicate(
    session: AsyncSession,
    *,
    agent_version_ids: list[UUID],
    profile_key: str,
    protocol_hash: str,
    result_metric: str | None,
    result_protocol_hash: str | None,
    force_retest: bool,
) -> None:
    statuses = set(
        await session.scalars(
            select(EvaluationRun.status)
            .join(RunAgent, RunAgent.run_id == EvaluationRun.id)
            .where(
                RunAgent.agent_version_id.in_(agent_version_ids),
                EvaluationRun.profile_key == profile_key,
                EvaluationRun.protocol_hash == protocol_hash,
            )
        )
    )
    active_statuses = {
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.PAUSED,
        RunStatus.SCORING,
    }
    if statuses & active_statuses:
        raise RunConfigurationError(
            "evaluation_already_active", "相同 Agent 和评测协议已有进行中的任务"
        )
    compatible_result_exists = False
    if result_metric is not None and result_protocol_hash is not None:
        compatible_result_exists = (
            await session.scalar(
                select(LeaderboardResult.id)
                .where(
                    LeaderboardResult.agent_version_id.in_(agent_version_ids),
                    LeaderboardResult.metric == result_metric,
                    LeaderboardResult.protocol_hash == result_protocol_hash,
                )
                .limit(1)
            )
        ) is not None
    if (RunStatus.PUBLISHED in statuses or compatible_result_exists) and not force_retest:
        raise RunConfigurationError(
            "evaluation_result_exists", "相同 Agent 和评测协议已有已发布结果"
        )
