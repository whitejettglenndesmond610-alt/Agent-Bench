from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from agent_eval.api.schemas.leaderboards import (
    AgentLeaderboardDetailResponse,
    AgentScoreHistoryPoint,
    BenchmarkRanking,
    GlobalLeaderboardOverviewResponse,
    GlobalLeaderboardResponse,
    GlobalMetricProtocol,
    GlobalOverviewAgent,
    GlobalOverviewMetric,
    GlobalRankedAgent,
    GlobalUnrankedAgent,
    LeaderboardMetric,
)
from agent_eval.api.schemas.runs import (
    AgentEvaluationStatus,
    AgentMetricEvaluationStatus,
    AgentMetricStatus,
)
from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    Ability,
    AgentStatus,
    AgentVersionLifecycle,
    ResultValidityStatus,
    RunScope,
    RunStatus,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    DatasetVersion,
    EvaluationRun,
    LeaderboardResult,
    LeaderboardSnapshot,
    RunAgent,
    RunDatasetVersion,
)
from agent_eval.runs.profiles import (
    UNIFIED_V3_PROFILE_KEY,
    UNIFIED_V3_PROFILE_KEYS,
)

METRIC_LABELS: dict[str, str] = {
    "overall": "综合",
    Ability.REASONING.value: "推理",
    Ability.RETRIEVAL.value: "检索",
    Ability.DATA_REASONING.value: "数据推理",
    Ability.INSTRUCTION_FOLLOWING.value: "指令遵循",
    Ability.LONG_CONTEXT.value: "长上下文",
}
STATUS_METRICS: tuple[LeaderboardMetric, ...] = (
    "overall",
    "reasoning",
    "retrieval",
    "data_reasoning",
    "instruction_following",
    "long_context",
)


@dataclass(frozen=True)
class ResultSource:
    result: LeaderboardResult
    snapshot: LeaderboardSnapshot
    run: EvaluationRun
    version: AgentVersion


async def get_global_leaderboard(
    session: AsyncSession, metric: LeaderboardMetric
) -> GlobalLeaderboardResponse:
    latest = (
        await session.execute(
            select(LeaderboardResult, LeaderboardSnapshot, EvaluationRun)
            .join(LeaderboardSnapshot, LeaderboardSnapshot.id == LeaderboardResult.snapshot_id)
            .join(EvaluationRun, EvaluationRun.id == LeaderboardSnapshot.run_id)
            .join(
                RunAgent,
                (RunAgent.id == LeaderboardResult.run_agent_id)
                & (RunAgent.agent_version_id == LeaderboardResult.agent_version_id)
                & (RunAgent.run_id == EvaluationRun.id),
            )
            .join(
                AgentVersion,
                (AgentVersion.id == LeaderboardResult.agent_version_id)
                & (AgentVersion.agent_id == LeaderboardResult.agent_id),
            )
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .where(LeaderboardResult.metric == metric)
            .where(Agent.owner_user_id.is_(None))
            .where(RunAgent.validity_status == ResultValidityStatus.VALID)
            .where(EvaluationRun.status == RunStatus.PUBLISHED)
            .where(EvaluationRun.ranking_eligible.is_(True))
            .where(EvaluationRun.profile_key.in_(UNIFIED_V3_PROFILE_KEYS))
            .order_by(
                LeaderboardSnapshot.published_at.desc(),
                LeaderboardSnapshot.id.desc(),
                LeaderboardResult.id.desc(),
            )
            .limit(1)
        )
    ).one_or_none()

    agents = list(
        await session.scalars(
            select(Agent)
            .options(selectinload(Agent.versions))
            .where(Agent.status == AgentStatus.ENABLED)
            .where(Agent.owner_user_id.is_(None))
            .order_by(Agent.name, Agent.id)
        )
    )
    latest_enabled_versions = {
        agent.id: max(
            (
                version
                for version in agent.versions
                if version.enabled
                and version.lifecycle_status == AgentVersionLifecycle.ACTIVE
            ),
            key=lambda version: (version.version, str(version.id)),
            default=None,
        )
        for agent in agents
    }

    if latest is None:
        return _response_without_results(metric, agents, latest_enabled_versions)

    latest_result, latest_snapshot, latest_run = latest
    rows = (
        await session.execute(
            select(
                LeaderboardResult,
                LeaderboardSnapshot,
                EvaluationRun,
                AgentVersion,
            )
            .options(joinedload(AgentVersion.agent))
            .join(LeaderboardSnapshot, LeaderboardSnapshot.id == LeaderboardResult.snapshot_id)
            .join(EvaluationRun, EvaluationRun.id == LeaderboardSnapshot.run_id)
            .join(
                AgentVersion,
                (AgentVersion.id == LeaderboardResult.agent_version_id)
                & (AgentVersion.agent_id == LeaderboardResult.agent_id),
            )
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .join(
                RunAgent,
                (RunAgent.id == LeaderboardResult.run_agent_id)
                & (RunAgent.agent_version_id == LeaderboardResult.agent_version_id)
                & (RunAgent.run_id == EvaluationRun.id),
            )
            .where(
                LeaderboardResult.metric == metric,
                LeaderboardResult.protocol_hash == latest_result.protocol_hash,
                EvaluationRun.status == RunStatus.PUBLISHED,
                EvaluationRun.ranking_eligible.is_(True),
                EvaluationRun.profile_key.in_(UNIFIED_V3_PROFILE_KEYS),
                RunAgent.validity_status == ResultValidityStatus.VALID,
                Agent.owner_user_id.is_(None),
            )
        )
    ).all()
    enabled_agent_ids = {agent.id for agent in agents}
    selected: dict[UUID, ResultSource] = {}
    for result, snapshot, run, version in rows:
        if result.agent_id not in enabled_agent_ids:
            continue
        candidate = ResultSource(result=result, snapshot=snapshot, run=run, version=version)
        current = selected.get(result.agent_id)
        if current is None or _source_order(candidate) > _source_order(current):
            selected[result.agent_id] = candidate

    ordered = sorted(
        selected.values(),
        key=lambda source: (
            -source.result.score,
            source.version.agent.name.casefold(),
            str(source.result.agent_id),
        ),
    )
    ranked: list[GlobalRankedAgent] = []
    previous_score: Decimal | None = None
    rank = 0
    for position, source in enumerate(ordered, start=1):
        if previous_score is None or source.result.score != previous_score:
            rank = position
            previous_score = source.result.score
        ranking = _snapshot_ranking(source.snapshot, source.result.agent_version_id)
        benchmarks = _benchmark_scores(ranking, metric)
        configured = latest_enabled_versions[source.result.agent_id]
        ranked.append(
            GlobalRankedAgent(
                rank=rank,
                score=float(source.result.score),
                agent_id=source.result.agent_id,
                agent_version_id=source.result.agent_version_id,
                agent_name=source.version.agent.name,
                provider=source.version.agent.provider,
                version=source.version.version,
                status=(
                    "current"
                    if configured is not None and configured.id == source.result.agent_version_id
                    else "needs_retest"
                ),
                source_snapshot_id=source.snapshot.id,
                source_run_id=source.run.id,
                source_run_name=source.run.name,
                source_profile_key=source.run.profile_key,
                source_published_at=source.snapshot.published_at,
                benchmark_count=source.result.benchmark_count,
                success_rate=float(ranking.get("success_rate", 0)),
                anomaly_rate=float(ranking.get("anomaly_rate", 0)),
                http_call_count=int(ranking.get("http_call_count", 0)),
                total_input_tokens=_optional_nonnegative_int(
                    ranking.get("total_input_tokens")
                ),
                total_output_tokens=_optional_nonnegative_int(
                    ranking.get("total_output_tokens")
                ),
                total_tokens=_optional_nonnegative_int(ranking.get("total_tokens")),
                token_usage_available_count=_nonnegative_int(
                    ranking.get("token_usage_available_count")
                ),
                token_usage_missing_count=_nonnegative_int(
                    ranking.get("token_usage_missing_count")
                ),
                latency_p50_ms=_optional_nonnegative_int(ranking.get("latency_p50_ms")),
                latency_p95_ms=_optional_nonnegative_int(ranking.get("latency_p95_ms")),
                latency_sample_count=_nonnegative_int(ranking.get("latency_sample_count")),
                pass_at_1=_optional_score(source.result.pass_at_1),
                pass_at_k=_optional_score(source.result.pass_at_k),
                pass_pow_k=_optional_score(source.result.pass_pow_k),
                ci_low=_optional_score(source.result.ci_low),
                ci_high=_optional_score(source.result.ci_high),
                benchmark_scores=benchmarks,
            )
        )

    datasets = list(
        await session.scalars(
            select(DatasetVersion)
            .options(joinedload(DatasetVersion.benchmark))
            .join(RunDatasetVersion, RunDatasetVersion.dataset_version_id == DatasetVersion.id)
            .where(RunDatasetVersion.run_id == latest_run.id)
        )
    )
    if metric != "overall":
        datasets = [item for item in datasets if item.benchmark.ability.value == metric]
    unranked = [
        _unranked_agent(agent, latest_enabled_versions[agent.id])
        for agent in agents
        if agent.id not in selected
    ]
    return GlobalLeaderboardResponse(
        metric=metric,
        label=METRIC_LABELS[metric],
        protocol_hash=latest_result.protocol_hash,
        generated_at=utc_now(),
        protocol_published_at=latest_snapshot.published_at,
        eligible_agent_count=len(agents),
        benchmark_count=len(datasets),
        question_count=sum(dataset.question_count for dataset in datasets),
        ranked_agents=ranked,
        unranked_agents=unranked,
    )


async def get_agent_evaluation_statuses(session: AsyncSession) -> list[AgentEvaluationStatus]:
    leaderboards = {
        metric: await get_global_leaderboard(session, metric) for metric in STATUS_METRICS
    }
    return await _agent_evaluation_statuses(session, leaderboards)


async def get_global_leaderboard_overview(
    session: AsyncSession,
) -> GlobalLeaderboardOverviewResponse:
    leaderboards = {
        metric: await get_global_leaderboard(session, metric) for metric in STATUS_METRICS
    }
    evaluation_statuses = await _agent_evaluation_statuses(session, leaderboards)
    status_by_agent = {status.agent_id: status for status in evaluation_statuses}
    ranked_by_metric = {
        metric: {result.agent_id: result for result in leaderboard.ranked_agents}
        for metric, leaderboard in leaderboards.items()
    }

    overall = leaderboards["overall"]
    ordered_agent_ids = [result.agent_id for result in overall.ranked_agents]
    ordered_agent_ids.extend(
        status.agent_id
        for status in evaluation_statuses
        if status.agent_id not in set(ordered_agent_ids)
    )
    ordered_agent_ids.extend(
        agent.agent_id
        for agent in overall.unranked_agents
        if agent.agent_id not in set(ordered_agent_ids)
    )
    fallback_identities: dict[UUID, tuple[UUID | None, str, str, int | None]] = {}
    for leaderboard in leaderboards.values():
        for ranked_agent in leaderboard.ranked_agents:
            fallback_identities[ranked_agent.agent_id] = (
                ranked_agent.agent_version_id,
                ranked_agent.agent_name,
                ranked_agent.provider,
                ranked_agent.version,
            )
        for unranked_agent in leaderboard.unranked_agents:
            fallback_identities[unranked_agent.agent_id] = (
                unranked_agent.agent_version_id,
                unranked_agent.agent_name,
                unranked_agent.provider,
                unranked_agent.version,
            )

    agents: list[GlobalOverviewAgent] = []
    for agent_id in ordered_agent_ids:
        evaluation = status_by_agent.get(agent_id)
        fallback_version_id, fallback_name, fallback_provider, fallback_version = (
            fallback_identities[agent_id]
        )
        metrics: dict[LeaderboardMetric, GlobalOverviewMetric] = {}
        for metric in STATUS_METRICS:
            result = ranked_by_metric[metric].get(agent_id)
            metric_status = (
                _evaluation_metric(evaluation, metric).status
                if evaluation is not None
                else AgentMetricStatus(status="not_evaluated").status
            )
            metrics[metric] = GlobalOverviewMetric(
                status=metric_status,
                message=(
                    _evaluation_metric(evaluation, metric).message
                    if evaluation is not None
                    else None
                ),
                rank=result.rank if result is not None else None,
                score=result.score if result is not None else None,
                source_agent_version_id=(
                    result.agent_version_id if result is not None else None
                ),
                source_version=result.version if result is not None else None,
                source_snapshot_id=result.source_snapshot_id if result is not None else None,
                source_run_id=result.source_run_id if result is not None else None,
                source_run_name=result.source_run_name if result is not None else None,
                source_profile_key=result.source_profile_key if result is not None else None,
                source_published_at=result.source_published_at if result is not None else None,
                benchmark_count=result.benchmark_count if result is not None else 0,
                success_rate=result.success_rate if result is not None else None,
                anomaly_rate=result.anomaly_rate if result is not None else None,
                http_call_count=result.http_call_count if result is not None else None,
                total_input_tokens=(
                    result.total_input_tokens if result is not None else None
                ),
                total_output_tokens=(
                    result.total_output_tokens if result is not None else None
                ),
                total_tokens=result.total_tokens if result is not None else None,
                token_usage_available_count=(
                    result.token_usage_available_count if result is not None else 0
                ),
                token_usage_missing_count=(
                    result.token_usage_missing_count if result is not None else 0
                ),
                latency_p50_ms=result.latency_p50_ms if result is not None else None,
                latency_p95_ms=result.latency_p95_ms if result is not None else None,
                latency_sample_count=(
                    result.latency_sample_count if result is not None else 0
                ),
                pass_at_1=result.pass_at_1 if result is not None else None,
                pass_at_k=result.pass_at_k if result is not None else None,
                pass_pow_k=result.pass_pow_k if result is not None else None,
                ci_low=result.ci_low if result is not None else None,
                ci_high=result.ci_high if result is not None else None,
                benchmark_scores=result.benchmark_scores if result is not None else [],
            )
        agents.append(
            GlobalOverviewAgent(
                agent_id=agent_id,
                agent_version_id=(
                    evaluation.agent_version_id
                    if evaluation is not None
                    else fallback_version_id
                ),
                agent_name=(
                    evaluation.agent_name if evaluation is not None else fallback_name
                ),
                provider=evaluation.provider if evaluation is not None else fallback_provider,
                version=evaluation.version if evaluation is not None else fallback_version,
                metrics=metrics,
            )
        )

    return GlobalLeaderboardOverviewResponse(
        generated_at=utc_now(),
        eligible_agent_count=len(agents),
        protocols=[
            GlobalMetricProtocol(
                metric=metric,
                label=leaderboard.label,
                protocol_hash=leaderboard.protocol_hash,
                published_at=leaderboard.protocol_published_at,
                benchmark_count=leaderboard.benchmark_count,
                question_count=leaderboard.question_count,
                ranked_agent_count=len(leaderboard.ranked_agents),
            )
            for metric, leaderboard in leaderboards.items()
        ],
        agents=agents,
    )


async def get_agent_leaderboard_detail(
    session: AsyncSession,
    agent_id: UUID,
    *,
    history_limit: int,
) -> AgentLeaderboardDetailResponse | None:
    agent = await session.scalar(
        select(Agent)
        .options(selectinload(Agent.versions))
        .where(
            Agent.id == agent_id,
            Agent.status == AgentStatus.ENABLED,
            Agent.owner_user_id.is_(None),
        )
    )
    if agent is None:
        return None

    current_version = max(
        (
            version
            for version in agent.versions
            if version.enabled and version.lifecycle_status == AgentVersionLifecycle.ACTIVE
        ),
        key=lambda version: (version.version, str(version.id)),
        default=None,
    )
    overview = await get_global_leaderboard_overview(session)
    overview_agent = next(
        (item for item in overview.agents if item.agent_id == agent_id),
        None,
    )
    if overview_agent is None:
        return None

    protocols = {item.metric: item.protocol_hash for item in overview.protocols}
    history = await _agent_score_history(
        session,
        agent_id,
        history_limit=history_limit,
        current_metrics=overview_agent.metrics,
        current_protocols=protocols,
    )
    return AgentLeaderboardDetailResponse(
        agent_id=agent.id,
        agent_version_id=current_version.id if current_version is not None else None,
        agent_name=agent.name,
        provider=agent.provider,
        description=agent.description,
        logo_url=agent.logo_url,
        version=current_version.version if current_version is not None else None,
        metrics=overview_agent.metrics,
        score_history=history,
    )


async def _agent_score_history(
    session: AsyncSession,
    agent_id: UUID,
    *,
    history_limit: int,
    current_metrics: dict[LeaderboardMetric, GlobalOverviewMetric],
    current_protocols: dict[LeaderboardMetric, str | None],
) -> dict[LeaderboardMetric, list[AgentScoreHistoryPoint]]:
    ranked_result_ids = (
        select(
            LeaderboardResult.id.label("result_id"),
            func.row_number()
            .over(
                partition_by=LeaderboardResult.metric,
                order_by=(
                    LeaderboardSnapshot.published_at.desc(),
                    LeaderboardSnapshot.id.desc(),
                    LeaderboardResult.id.desc(),
                ),
            )
            .label("history_rank"),
        )
        .select_from(LeaderboardResult)
        .join(LeaderboardSnapshot, LeaderboardSnapshot.id == LeaderboardResult.snapshot_id)
        .join(EvaluationRun, EvaluationRun.id == LeaderboardSnapshot.run_id)
        .join(
            RunAgent,
            (RunAgent.id == LeaderboardResult.run_agent_id)
            & (RunAgent.agent_version_id == LeaderboardResult.agent_version_id)
            & (RunAgent.run_id == EvaluationRun.id),
        )
        .join(
            AgentVersion,
            (AgentVersion.id == LeaderboardResult.agent_version_id)
            & (AgentVersion.agent_id == LeaderboardResult.agent_id),
        )
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .where(
            LeaderboardResult.agent_id == agent_id,
            LeaderboardResult.metric.in_(STATUS_METRICS),
            RunAgent.validity_status == ResultValidityStatus.VALID,
            EvaluationRun.status == RunStatus.PUBLISHED,
            EvaluationRun.ranking_eligible.is_(True),
            Agent.owner_user_id.is_(None),
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                LeaderboardResult,
                LeaderboardSnapshot,
                EvaluationRun,
                AgentVersion,
            )
            .join(
                ranked_result_ids,
                ranked_result_ids.c.result_id == LeaderboardResult.id,
            )
            .join(LeaderboardSnapshot, LeaderboardSnapshot.id == LeaderboardResult.snapshot_id)
            .join(EvaluationRun, EvaluationRun.id == LeaderboardSnapshot.run_id)
            .join(AgentVersion, AgentVersion.id == LeaderboardResult.agent_version_id)
            .where(ranked_result_ids.c.history_rank <= history_limit)
            .order_by(
                LeaderboardResult.metric,
                LeaderboardSnapshot.published_at,
                LeaderboardSnapshot.id,
                LeaderboardResult.id,
            )
        )
    ).all()

    history: dict[LeaderboardMetric, list[AgentScoreHistoryPoint]] = {
        metric: [] for metric in STATUS_METRICS
    }
    for result, snapshot, run, version in rows:
        metric = cast(LeaderboardMetric, result.metric)
        current_metric = current_metrics[metric]
        history[metric].append(
            AgentScoreHistoryPoint(
                score=float(result.score),
                protocol_hash=result.protocol_hash,
                protocol_matches_current=(
                    current_protocols[metric] is not None
                    and result.protocol_hash == current_protocols[metric]
                ),
                is_current_source=(
                    current_metric.source_snapshot_id == snapshot.id
                    and current_metric.source_agent_version_id == version.id
                ),
                agent_version_id=version.id,
                version=version.version,
                source_snapshot_id=snapshot.id,
                source_run_id=run.id,
                source_run_name=run.name,
                source_profile_key=run.profile_key,
                source_published_at=snapshot.published_at,
                benchmark_count=result.benchmark_count,
            )
        )
    return history


async def _agent_evaluation_statuses(
    session: AsyncSession,
    leaderboards: dict[LeaderboardMetric, GlobalLeaderboardResponse],
) -> list[AgentEvaluationStatus]:
    agents = list(
        await session.scalars(
            select(Agent)
            .options(selectinload(Agent.versions))
            .where(Agent.status == AgentStatus.ENABLED)
            .where(Agent.owner_user_id.is_(None))
        )
    )
    identities = {
        agent.id: (agent, version)
        for agent in agents
        if (
            version := max(
                (
                    item
                    for item in agent.versions
                    if item.enabled
                    and item.lifecycle_status == AgentVersionLifecycle.ACTIVE
                ),
                key=lambda item: (item.version, str(item.id)),
                default=None,
            )
        )
        is not None
    }
    statuses: dict[UUID, dict[str, AgentMetricStatus]] = {
        agent_id: {metric: AgentMetricStatus(status="not_evaluated") for metric in STATUS_METRICS}
        for agent_id in identities
    }
    for metric, leaderboard in leaderboards.items():
        for result in leaderboard.ranked_agents:
            if result.agent_id not in statuses:
                continue
            statuses[result.agent_id][metric] = AgentMetricStatus(
                status="evaluated" if result.status == "current" else "needs_retest",
                score=result.score,
                run_id=result.source_run_id,
                source_version=result.version,
                updated_at=result.source_published_at,
            )

    latest_version_ids = {version.id: agent.id for agent, version in identities.values()}
    run_agent_count = (
        select(func.count())
        .select_from(RunAgent)
        .where(RunAgent.run_id == EvaluationRun.id)
        .correlate(EvaluationRun)
        .scalar_subquery()
    )
    active_rows = (
        await session.execute(
            select(EvaluationRun, RunAgent)
            .join(RunAgent, RunAgent.run_id == EvaluationRun.id)
            .where(
                RunAgent.agent_version_id.in_(latest_version_ids),
                EvaluationRun.status.in_(
                    [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.SCORING]
                ),
                run_agent_count == 1,
                EvaluationRun.ranking_eligible.is_(True),
                EvaluationRun.profile_key.in_(UNIFIED_V3_PROFILE_KEYS),
            )
            .order_by(EvaluationRun.created_at, EvaluationRun.id)
        )
    ).all()
    failed_rows = (
        await session.execute(
            select(EvaluationRun, RunAgent)
            .join(RunAgent, RunAgent.run_id == EvaluationRun.id)
            .where(
                RunAgent.agent_version_id.in_(latest_version_ids),
                EvaluationRun.status == RunStatus.SYSTEM_FAILED,
                EvaluationRun.ranking_eligible.is_(True),
                EvaluationRun.profile_key.in_(UNIFIED_V3_PROFILE_KEYS),
            )
            .order_by(EvaluationRun.created_at, EvaluationRun.id)
        )
    ).all()
    invalid_rows = (
        await session.execute(
            select(EvaluationRun, RunAgent, LeaderboardSnapshot)
            .join(RunAgent, RunAgent.run_id == EvaluationRun.id)
            .join(LeaderboardSnapshot, LeaderboardSnapshot.run_id == EvaluationRun.id)
            .where(
                RunAgent.agent_version_id.in_(latest_version_ids),
                RunAgent.validity_status == ResultValidityStatus.INVALID,
                EvaluationRun.status == RunStatus.PUBLISHED,
                EvaluationRun.ranking_eligible.is_(True),
                EvaluationRun.profile_key.in_(UNIFIED_V3_PROFILE_KEYS),
            )
            .order_by(LeaderboardSnapshot.published_at, LeaderboardSnapshot.id)
        )
    ).all()
    valid_rows = (
        await session.execute(
            select(
                RunAgent.agent_version_id,
                LeaderboardResult.metric,
                LeaderboardSnapshot.published_at,
            )
            .join(
                LeaderboardResult,
                LeaderboardResult.run_agent_id == RunAgent.id,
            )
            .join(LeaderboardSnapshot, LeaderboardSnapshot.id == LeaderboardResult.snapshot_id)
            .join(EvaluationRun, EvaluationRun.id == LeaderboardSnapshot.run_id)
            .where(
                RunAgent.agent_version_id.in_(latest_version_ids),
                RunAgent.validity_status == ResultValidityStatus.VALID,
                EvaluationRun.status == RunStatus.PUBLISHED,
                EvaluationRun.ranking_eligible.is_(True),
                EvaluationRun.profile_key.in_(UNIFIED_V3_PROFILE_KEYS),
            )
        )
    ).all()

    for run, run_agent in failed_rows:
        agent_id = latest_version_ids[run_agent.agent_version_id]
        _, version = identities[agent_id]
        for affected_metric in _affected_metrics(run):
            current = statuses[agent_id][affected_metric]
            if current.score is None:
                statuses[agent_id][affected_metric] = AgentMetricStatus(
                    status="failed",
                    run_id=run.id,
                    source_version=version.version,
                    updated_at=run.finished_at or run.heartbeat_at or run.created_at,
                )

    latest_invalid: dict[tuple[UUID, str], tuple[EvaluationRun, RunAgent, datetime]] = {}
    for run, run_agent, snapshot in invalid_rows:
        agent_id = latest_version_ids[run_agent.agent_version_id]
        for affected_metric in _affected_metrics(run):
            latest_invalid[(agent_id, affected_metric)] = (
                run,
                run_agent,
                snapshot.published_at,
            )
    latest_valid_at: dict[tuple[UUID, str], datetime] = {}
    for agent_version_id, metric, published_at in valid_rows:
        agent_id = latest_version_ids[agent_version_id]
        key = (agent_id, metric)
        if key not in latest_valid_at or published_at > latest_valid_at[key]:
            latest_valid_at[key] = published_at
    for (agent_id, affected_metric), (run, run_agent, published_at) in latest_invalid.items():
        _, version = identities[agent_id]
        current = statuses[agent_id][affected_metric]
        current_valid_at = latest_valid_at.get((agent_id, affected_metric))
        has_newer_current_valid = current_valid_at is not None and current_valid_at > published_at
        current_event_is_newer = (
            current.status == "failed"
            and current.updated_at is not None
            and current.updated_at > published_at
        )
        if not has_newer_current_valid and not current_event_is_newer:
            statuses[agent_id][affected_metric] = AgentMetricStatus(
                status="invalid",
                run_id=run.id,
                source_version=version.version,
                updated_at=published_at,
                message=run_agent.validity_reason,
            )

    for run, run_agent in active_rows:
        agent_id = latest_version_ids[run_agent.agent_version_id]
        _, version = identities[agent_id]
        run_status: AgentMetricEvaluationStatus = (
            "queued" if run.status == RunStatus.QUEUED else "running"
        )
        for affected_metric in _affected_metrics(run):
            statuses[agent_id][affected_metric] = AgentMetricStatus(
                status=run_status,
                run_id=run.id,
                source_version=version.version,
                updated_at=run.heartbeat_at or run.started_at or run.created_at,
            )

    return [
        AgentEvaluationStatus(
            agent_id=agent.id,
            agent_version_id=version.id,
            agent_name=agent.name,
            provider=agent.provider,
            version=version.version,
            max_prompt_characters=version.max_prompt_characters,
            supports_citations=version.citations_jsonpath is not None,
            comprehensive=statuses[agent.id]["overall"],
            reasoning=statuses[agent.id][Ability.REASONING.value],
            retrieval=statuses[agent.id][Ability.RETRIEVAL.value],
            data_reasoning=statuses[agent.id][Ability.DATA_REASONING.value],
            instruction_following=statuses[agent.id][Ability.INSTRUCTION_FOLLOWING.value],
            long_context=statuses[agent.id][Ability.LONG_CONTEXT.value],
        )
        for agent, version in sorted(
            identities.values(), key=lambda item: (item[0].name.casefold(), item[0].id)
        )
    ]


def _evaluation_metric(
    evaluation: AgentEvaluationStatus, metric: LeaderboardMetric
) -> AgentMetricStatus:
    if metric == "overall":
        return evaluation.comprehensive
    if metric == Ability.REASONING.value:
        return evaluation.reasoning
    if metric == Ability.RETRIEVAL.value:
        return evaluation.retrieval
    if metric == Ability.DATA_REASONING.value:
        return evaluation.data_reasoning
    if metric == Ability.INSTRUCTION_FOLLOWING.value:
        return evaluation.instruction_following
    return evaluation.long_context


def _affected_metrics(run: EvaluationRun) -> tuple[str, ...]:
    if run.run_scope == RunScope.COMPREHENSIVE:
        return (
            STATUS_METRICS
            if run.profile_key == UNIFIED_V3_PROFILE_KEY
            else ("overall", "reasoning", "retrieval", "data_reasoning")
        )
    if run.run_scope == RunScope.SPECIALIST and run.target_ability is not None:
        return (run.target_ability.value,)
    return ()


def _source_order(source: ResultSource) -> tuple[int, datetime, str]:
    return (
        source.version.version,
        source.snapshot.published_at,
        str(source.snapshot.id),
    )


def _snapshot_ranking(snapshot: LeaderboardSnapshot, agent_version_id: UUID) -> dict[str, Any]:
    for ranking in snapshot.rankings_json:
        if str(ranking.get("agent_version_id")) == str(agent_version_id):
            return ranking
    return {}


def _benchmark_scores(ranking: dict[str, Any], metric: LeaderboardMetric) -> list[BenchmarkRanking]:
    scores = ranking.get("benchmark_scores")
    if not isinstance(scores, list):
        return []
    benchmarks: list[BenchmarkRanking] = []
    for score in scores:
        if not isinstance(score, dict) or (metric != "overall" and score.get("ability") != metric):
            continue
        try:
            benchmarks.append(BenchmarkRanking.model_validate(score))
        except ValidationError:
            continue
    return benchmarks


def _unranked_agent(agent: Agent, version: AgentVersion | None) -> GlobalUnrankedAgent:
    return GlobalUnrankedAgent(
        agent_id=agent.id,
        agent_version_id=version.id if version is not None else None,
        agent_name=agent.name,
        provider=agent.provider,
        version=version.version if version is not None else None,
    )


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _optional_score(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _nonnegative_int(value: object) -> int:
    return _optional_nonnegative_int(value) or 0


def _response_without_results(
    metric: LeaderboardMetric,
    agents: list[Agent],
    latest_enabled_versions: dict[UUID, AgentVersion | None],
) -> GlobalLeaderboardResponse:
    return GlobalLeaderboardResponse(
        metric=metric,
        label=METRIC_LABELS[metric],
        protocol_hash=None,
        generated_at=utc_now(),
        protocol_published_at=None,
        eligible_agent_count=len(agents),
        benchmark_count=0,
        question_count=0,
        ranked_agents=[],
        unranked_agents=[
            _unranked_agent(agent, latest_enabled_versions[agent.id]) for agent in agents
        ],
    )
