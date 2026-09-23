from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload, selectinload

from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    Ability,
    AttemptStatus,
    CitationFetchStatus,
    DesiredRunState,
    ExecutionStatus,
    NotificationType,
    ResultValidityStatus,
    RunAgentStatus,
    RunScope,
    RunStage,
    RunStatus,
)
from agent_eval.db.models import (
    AgentVersion,
    Benchmark,
    DatasetVersion,
    EvaluationRun,
    ExecutionItem,
    JudgeConfig,
    LeaderboardResult,
    LeaderboardSnapshot,
    Notification,
    Question,
    RunAgent,
    RunDatasetVersion,
    ScoreConfig,
)
from agent_eval.leaderboards.validity import classify_result_validity
from agent_eval.runs.profiles import UNIFIED_V3_PROFILE_KEY
from agent_eval.runs.queue import acquire_scheduler_lock, lock_owned_run
from agent_eval.runs.service import metric_protocol_hash
from agent_eval.scoring.aggregation import (
    AgentAggregate,
    AggregateAttempt,
    AggregateItem,
    aggregate_agent,
    build_rankings,
)
from agent_eval.scoring.configs import ability_weights
from agent_eval.scoring.pricing import resolve_agent_pricing

TERMINAL_ITEM_STATUSES = {ExecutionStatus.SCORED, ExecutionStatus.FAILED}
TERMINAL_CITATION_STATUSES = {
    CitationFetchStatus.FETCHED,
    CitationFetchStatus.BLOCKED,
    CitationFetchStatus.UNREACHABLE,
    CitationFetchStatus.UNSUPPORTED,
}


async def execute_aggregation_and_publish(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    *,
    owner_id: str,
) -> bool:
    async with session_factory() as session:
        try:
            await acquire_scheduler_lock(session)
            return await aggregate_and_publish(session, run_id, owner_id=owner_id)
        except BaseException:
            await session.rollback()
            raise


async def execute_aggregation_and_publish_direct(
    session_factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> bool:
    """Run unfenced aggregation for tests that do not execute through a worker."""
    async with session_factory() as session:
        try:
            await acquire_scheduler_lock(session)
            return await _aggregate_and_publish(session, run_id, owner_id=None)
        except BaseException:
            await session.rollback()
            raise


async def aggregate_and_publish(
    session: AsyncSession, run_id: UUID, *, owner_id: str
) -> bool:
    return await _aggregate_and_publish(session, run_id, owner_id=owner_id)


async def _aggregate_and_publish(
    session: AsyncSession, run_id: UUID, *, owner_id: str | None
) -> bool:
    run = (
        await lock_owned_run(session, run_id, owner_id)
        if owner_id is not None
        else await session.scalar(
            select(EvaluationRun).where(EvaluationRun.id == run_id).with_for_update()
        )
    )
    if run is None:
        return False
    if run.status == RunStatus.PUBLISHED:
        snapshot = await session.scalar(
            select(LeaderboardSnapshot).where(LeaderboardSnapshot.run_id == run_id)
        )
        return snapshot is not None
    if run.desired_state != DesiredRunState.RUNNING:
        return False
    if (
        run.status != RunStatus.SCORING
        or run.stage != RunStage.AGGREGATING
        or run.system_error is not None
    ):
        raise RuntimeError("evaluation run is not ready for aggregation")

    score_config = (
        await session.get(ScoreConfig, run.score_config_id)
        if run.score_config_id is not None
        else None
    )
    if run.score_config_id is not None and score_config is None:
        raise RuntimeError("score configuration is unavailable")
    judge_config = await session.get(JudgeConfig, run.judge_config_id)
    if judge_config is None:
        raise RuntimeError("judge configuration is unavailable")
    run_agents = list(
        await session.scalars(
            select(RunAgent)
            .options(
                joinedload(RunAgent.agent_version).joinedload(AgentVersion.agent),
                selectinload(RunAgent.execution_items)
                .joinedload(ExecutionItem.question)
                .joinedload(Question.dataset_version)
                .joinedload(DatasetVersion.benchmark),
                selectinload(RunAgent.execution_items).joinedload(ExecutionItem.score),
                selectinload(RunAgent.execution_items).selectinload(ExecutionItem.citations),
                selectinload(RunAgent.execution_items).selectinload(ExecutionItem.attempts),
            )
            .where(RunAgent.run_id == run_id)
            .order_by(RunAgent.id)
        )
    )
    validate_publishable(run_agents)
    for run_agent in run_agents:
        validity = classify_result_validity(item.error_type for item in run_agent.execution_items)
        run_agent.validity_status = validity.status
        run_agent.validity_reason = validity.reason
        run_agent.configuration_failure_count = validity.configuration_failure_count
        run_agent.configuration_failure_rate = validity.configuration_failure_rate
    aggregates = [aggregate_run_agent(run_agent, score_config) for run_agent in run_agents]
    valid_aggregates = [
        aggregate
        for aggregate in aggregates
        if aggregate.validity_status == ResultValidityStatus.VALID
    ]
    rankings = build_rankings(aggregates)

    dataset_versions = list(
        await session.scalars(
            select(DatasetVersion)
            .options(joinedload(DatasetVersion.benchmark))
            .join(
                RunDatasetVersion,
                RunDatasetVersion.dataset_version_id == DatasetVersion.id,
            )
            .where(RunDatasetVersion.run_id == run_id)
        )
    )

    existing = await session.scalar(
        select(LeaderboardSnapshot).where(LeaderboardSnapshot.run_id == run_id)
    )
    if existing is not None:
        raise RuntimeError("evaluation run already has a non-current snapshot")
    replace_current = (
        run.ranking_eligible
        and run.run_scope == RunScope.COMPREHENSIVE
        and run.profile_key == UNIFIED_V3_PROFILE_KEY
        and bool(valid_aggregates)
    )
    if replace_current:
        await session.execute(
            update(LeaderboardSnapshot)
            .where(LeaderboardSnapshot.is_current.is_(True))
            .values(is_current=False)
        )
        await session.flush()
    now = utc_now()
    snapshot = LeaderboardSnapshot(
        run_id=run_id,
        published_at=now,
        rankings_json=rankings,
        is_current=replace_current,
    )
    session.add(snapshot)
    await session.flush()
    if run.ranking_eligible:
        if score_config is None:
            raise RuntimeError("ranking-eligible runs require a score configuration")
        covered_abilities = {
            benchmark.ability
            for aggregate in valid_aggregates
            for benchmark in aggregate.benchmark_scores
        }
        metrics = (
            ["overall", *(ability.value for ability in Ability if ability in covered_abilities)]
            if run.run_scope == RunScope.COMPREHENSIVE
            else [run.target_ability.value]
            if run.run_scope == RunScope.SPECIALIST and run.target_ability in covered_abilities
            else []
        )
        protocol_hashes = {
            metric: metric_protocol_hash(
                metric=metric,
                dataset_versions=dataset_versions,
                judge_config=judge_config,
                score_config=score_config,
                profile_key=run.profile_key,
            )
            for metric in metrics
        }
        session.add_all(
            [
                LeaderboardResult(
                    snapshot_id=snapshot.id,
                    run_agent_id=aggregate.run_agent_id,
                    agent_id=aggregate.agent_id,
                    agent_version_id=aggregate.agent_version_id,
                    metric=metric,
                    score=(
                        aggregate.overall_score
                        if metric == "overall"
                        else aggregate.ability_scores[Ability(metric)]
                    ),
                    protocol_hash=protocol_hashes[metric],
                    benchmark_count=sum(
                        metric == "overall" or benchmark.ability.value == metric
                        for benchmark in aggregate.benchmark_scores
                    ),
                    ci_low=aggregate.ci_low,
                    ci_high=aggregate.ci_high,
                    pass_at_1=aggregate.pass_at_1,
                    pass_at_k=aggregate.pass_at_k,
                    pass_pow_k=aggregate.pass_pow_k,
                    total_cost_usd=aggregate.total_cost_usd,
                    cost_per_task_usd=aggregate.cost_per_task_usd,
                )
                for aggregate in valid_aggregates
                for metric in metrics
            ]
        )
    session.add(
        Notification(
            type=NotificationType.RUN_PUBLISHED,
            title="评测榜单已发布",
            message=f"评测任务「{run.name}」已完成并发布最新榜单。",
        )
    )
    run.stage = RunStage.READY_TO_PUBLISH
    run.status = RunStatus.PUBLISHED
    run.active_slot = None
    run.claim_owner_id = None
    run.claimed_at = None
    run.heartbeat_at = now
    run.finished_at = now
    await session.commit()
    return True


def validate_publishable(run_agents: list[RunAgent]) -> None:
    if not run_agents:
        raise RuntimeError("evaluation run has no agents")
    for run_agent in run_agents:
        if run_agent.agent_version.agent.owner_user_id is not None:
            raise RuntimeError("private Agent results cannot be published")
        items = run_agent.execution_items
        if run_agent.status != RunAgentStatus.COMPLETED:
            raise RuntimeError("run agent is not complete")
        if len(items) != run_agent.total:
            raise RuntimeError("run agent execution item count is incomplete")
        for item in items:
            if item.status not in TERMINAL_ITEM_STATUSES or item.score is None:
                raise RuntimeError("execution item is not fully scored")
            if (
                item.status == ExecutionStatus.SCORED
                and item.question.requires_citations
                and item.score.citation_score is None
            ):
                raise RuntimeError("citation item has no citation score")
            for citation in item.citations:
                if (
                    citation.fetch_status not in TERMINAL_CITATION_STATUSES
                    or citation.authenticity_score is None
                    or citation.support_score is None
                ):
                    raise RuntimeError("citation is not fully scored")


def aggregate_run_agent(run_agent: RunAgent, score_config: ScoreConfig | None) -> AgentAggregate:
    version = run_agent.agent_version
    items: list[AggregateItem] = []
    for item in run_agent.execution_items:
        score = item.score
        if score is None:
            raise RuntimeError("execution item has no score")
        benchmark: Benchmark = item.question.dataset_version.benchmark
        items.append(
            AggregateItem(
                benchmark_id=benchmark.id,
                benchmark_name=benchmark.name,
                ability=benchmark.ability,
                language=benchmark.language,
                final_score=score.final_score,
                answer_score=score.answer_score,
                citation_score=score.citation_score,
                requires_citations=item.question.requires_citations,
                succeeded=item.status == ExecutionStatus.SCORED,
                call_count=item.attempt_count,
                attempts=tuple(
                    AggregateAttempt(
                        latency_ms=attempt.latency_ms,
                        input_tokens=attempt.input_tokens,
                        output_tokens=attempt.output_tokens,
                        total_tokens=attempt.total_tokens,
                    )
                    for attempt in item.attempts
                    if attempt.status != AttemptStatus.IN_PROGRESS
                ),
                question_id=item.question_id,
                trial_index=item.trial_index,
            )
        )
    if score_config is not None:
        weights = ability_weights(score_config)
    else:
        # Runs without a score configuration (tau2 interactive profile) carry a
        # single ability; weight it fully so the overall score is its mean.
        covered = {item.ability for item in items}
        if len(covered) != 1:
            raise RuntimeError(
                "runs without a score configuration must cover exactly one ability"
            )
        weights = {covered.pop(): 100}
    return aggregate_agent(
        run_agent_id=run_agent.id,
        agent_id=version.agent_id,
        agent_version_id=version.id,
        agent_name=version.agent.name,
        provider=version.agent.provider,
        version=version.version,
        items=items,
        ability_weights=weights,
        validity_status=run_agent.validity_status,
        validity_reason=run_agent.validity_reason,
        configuration_failure_count=run_agent.configuration_failure_count,
        configuration_failure_rate=run_agent.configuration_failure_rate,
        pricing=resolve_agent_pricing(
            private_preset=version.private_preset,
            private_model=version.private_model,
            request_template=version.request_template,
        ),
    )
