from collections import Counter, defaultdict
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.sql.base import ExecutableOption

from agent_eval.api.dependencies import DatabaseSession, OperatorCsrfSession, OperatorSession
from agent_eval.api.mutation_guard import sqlite_mutation_guard
from agent_eval.api.routes.runs import (
    resolve_profile,
    result_summary,
    run_name_for_agent,
)
from agent_eval.api.routes.workspace import request_fingerprint
from agent_eval.api.schemas.evaluation_experiments import (
    EvaluationExperimentCreate,
    EvaluationExperimentCreator,
    EvaluationExperimentDetail,
    EvaluationExperimentMember,
    EvaluationExperimentMetricComparison,
    EvaluationExperimentRunCounts,
    EvaluationExperimentSummary,
)
from agent_eval.api.schemas.runs import RunAgentResultSummary
from agent_eval.auth.identity import legacy_subject_key
from agent_eval.db.base import ensure_utc
from agent_eval.db.enums import Ability, RunStatus
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    EvaluationExperiment,
    EvaluationRun,
    LeaderboardSnapshot,
    RunAgent,
)
from agent_eval.db.models import Session as AuthSession
from agent_eval.runs.service import RunConfigurationError, prepare_evaluation_run

router = APIRouter(prefix="/api/v1/evaluation-experiments", tags=["evaluation-experiments"])


def experiment_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def creator_identity(auth_session: AuthSession) -> tuple[UUID | None, str, str]:
    user = auth_session.user
    if user is not None:
        return user.id, user.subject_key, user.display_name
    return None, legacy_subject_key(auth_session.role), "Shared operator"


def experiment_fingerprint(payload: EvaluationExperimentCreate) -> str:
    return request_fingerprint(
        {
            "agent_version_ids": [str(value) for value in payload.agent_version_ids],
            "dataset_version_ids": (
                [str(value) for value in payload.dataset_version_ids]
                if payload.dataset_version_ids is not None
                else None
            ),
            "force_retest": payload.force_retest,
            "judge_config_id": str(payload.judge_config_id),
            "name": payload.name,
            "operation": "create_evaluation_experiment",
            "profile_key": payload.profile_key,
            "score_config_id": str(payload.score_config_id),
        }
    )


def derived_status(runs: list[EvaluationRun]) -> RunStatus:
    statuses = {run.status for run in runs}
    if len(statuses) == 1:
        return next(iter(statuses))
    if RunStatus.RUNNING in statuses:
        return RunStatus.RUNNING
    if RunStatus.SCORING in statuses:
        return RunStatus.SCORING
    if RunStatus.PAUSED in statuses:
        return RunStatus.PAUSED
    if RunStatus.QUEUED in statuses or RunStatus.DRAFT in statuses:
        return RunStatus.QUEUED
    if RunStatus.SYSTEM_FAILED in statuses:
        return RunStatus.SYSTEM_FAILED
    if statuses == {RunStatus.CANCELED}:
        return RunStatus.CANCELED
    return RunStatus.COMPLETED


def summary_response(experiment: EvaluationExperiment) -> EvaluationExperimentSummary:
    runs = experiment.runs
    if not runs:
        raise RuntimeError("evaluation experiment has no member runs")
    run_counts = Counter(run.status for run in runs)
    total = sum(agent.total for run in runs for agent in run.run_agents)
    completed = sum(agent.completed for run in runs for agent in run.run_agents)
    failed = sum(agent.failed for run in runs for agent in run.run_agents)
    processed = completed + failed
    first_run = min(
        runs,
        key=lambda run: run.experiment_position if run.experiment_position is not None else 0,
    )
    updated_at = max(
        ensure_utc(value) for value in [experiment.updated_at, *(run.updated_at for run in runs)]
    )
    return EvaluationExperimentSummary(
        id=experiment.id,
        name=experiment.name,
        creator=EvaluationExperimentCreator(
            user_id=experiment.creator_user_id,
            subject_key=experiment.creator_subject_key,
            display_name=experiment.creator_display_name,
        ),
        status=derived_status(runs),
        protocol_hash=experiment.protocol_hash,
        profile_key=first_run.profile_key,
        run_scope=first_run.run_scope,
        target_ability=first_run.target_ability,
        member_count=len(runs),
        run_counts=EvaluationExperimentRunCounts(
            **{run_status.value: run_counts[run_status] for run_status in RunStatus}
        ),
        total=total,
        processed=processed,
        completed=completed,
        failed=failed,
        progress_percent=round(processed * 100 / total, 2) if total else 0,
        created_at=experiment.created_at,
        updated_at=updated_at,
    )


def experiment_summary_options() -> tuple[ExecutableOption, ...]:
    return (
        selectinload(EvaluationExperiment.runs).selectinload(
            EvaluationRun.run_agents
        ),
    )


def experiment_detail_options() -> tuple[ExecutableOption, ...]:
    return (
        selectinload(EvaluationExperiment.runs)
        .selectinload(EvaluationRun.run_agents)
        .joinedload(RunAgent.agent_version)
        .joinedload(AgentVersion.agent),
        selectinload(EvaluationExperiment.runs)
        .selectinload(EvaluationRun.snapshots)
        .selectinload(LeaderboardSnapshot.results),
    )


async def load_experiment(
    database: AsyncSession, experiment_id: UUID
) -> EvaluationExperiment | None:
    experiment: EvaluationExperiment | None = await database.scalar(
        select(EvaluationExperiment)
        .options(*experiment_detail_options())
        .where(EvaluationExperiment.id == experiment_id)
    )
    if experiment is not None and any(
        run_agent.agent_version.agent.owner_user_id is not None
        for run in experiment.runs
        for run_agent in run.run_agents
    ):
        return None
    return experiment


async def existing_experiment(
    database: AsyncSession,
    creator_subject_key: str,
    idempotency_key: str,
    fingerprint: str,
) -> EvaluationExperiment | None:
    experiment = await database.scalar(
        select(EvaluationExperiment).where(
            EvaluationExperiment.creator_subject_key == creator_subject_key,
            EvaluationExperiment.idempotency_key == idempotency_key,
        )
    )
    if experiment is not None and experiment.request_fingerprint != fingerprint:
        raise experiment_error(
            status.HTTP_409_CONFLICT,
            "idempotency_conflict",
            "Idempotency key was already used with a different experiment payload",
        )
    if experiment is None:
        return None
    return await load_experiment(database, experiment.id)


def detail_response(experiment: EvaluationExperiment) -> EvaluationExperimentDetail:
    parsed_results: dict[UUID, RunAgentResultSummary] = {}
    snapshots: dict[UUID, LeaderboardSnapshot] = {}
    ordered_runs = sorted(
        experiment.runs,
        key=lambda run: run.experiment_position if run.experiment_position is not None else 0,
    )
    for run in ordered_runs:
        if len(run.run_agents) != 1 or run.experiment_position is None:
            raise RuntimeError("evaluation experiment member has an invalid Agent relationship")
        run_agent = run.run_agents[0]
        if run.snapshots:
            snapshot = max(run.snapshots, key=lambda item: (item.published_at, item.id))
            snapshots[run.id] = snapshot
            for value in snapshot.rankings_json:
                if (
                    isinstance(value, dict)
                    and value.get("agent_version_id") == str(run_agent.agent_version_id)
                    and (parsed := result_summary(value)) is not None
                ):
                    parsed_results[run_agent.agent_version_id] = parsed
                    break

    metric_rows: dict[str, dict[str, list[tuple[UUID, Decimal]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for run in ordered_runs:
        stored_snapshot = snapshots.get(run.id)
        if stored_snapshot is None:
            continue
        for row in stored_snapshot.results:
            if row.agent_version_id in parsed_results:
                metric_rows[row.metric][row.protocol_hash].append(
                    (row.agent_version_id, row.score)
                )

    metric_scores: dict[UUID, dict[str, float]] = defaultdict(dict)
    metric_ranks: dict[UUID, dict[str, int]] = defaultdict(dict)
    comparisons: list[EvaluationExperimentMetricComparison] = []
    positions = {
        run.run_agents[0].agent_version_id: run.experiment_position or 0
        for run in ordered_runs
    }
    ability_metrics = {ability.value for ability in Ability}
    for metric, protocol_groups in metric_rows.items():
        if len(protocol_groups) != 1:
            continue
        protocol_hash, rows = next(iter(protocol_groups.items()))
        comparable: list[tuple[UUID, Decimal]] = []
        for version_id, _stored_score in rows:
            parsed = parsed_results[version_id]
            display_score = (
                parsed.overall_score
                if metric == "overall"
                else parsed.ability_scores.get(Ability(metric))
                if metric in ability_metrics
                else None
            )
            if display_score is not None:
                comparable.append((version_id, Decimal(str(display_score))))
                metric_scores[version_id][metric] = display_score
        comparable.sort(
            key=lambda item: (
                -item[1],
                positions[item[0]],
            )
        )
        previous_score: Decimal | None = None
        rank = 0
        for position, (version_id, metric_score) in enumerate(comparable, start=1):
            if previous_score is None or metric_score != previous_score:
                rank = position
                previous_score = metric_score
            metric_ranks[version_id][metric] = rank
        if comparable:
            comparisons.append(
                EvaluationExperimentMetricComparison(
                    metric=metric,
                    protocol_hash=protocol_hash,
                    compared_count=len(comparable),
                )
            )

    metric_order = {
        "overall": 0,
        **{ability.value: index + 1 for index, ability in enumerate(Ability)},
    }
    comparisons.sort(key=lambda item: (metric_order.get(item.metric, 100), item.metric))
    members = []
    for run in ordered_runs:
        run_agent = run.run_agents[0]
        processed = run_agent.completed + run_agent.failed
        members.append(
            EvaluationExperimentMember(
                position=run.experiment_position or 0,
                run_id=run.id,
                run_name=run.name,
                status=run.status,
                stage=run.stage,
                agent_id=run_agent.agent_version.agent_id,
                agent_version_id=run_agent.agent_version_id,
                agent_name=run_agent.agent_version.agent.name,
                provider=run_agent.agent_version.agent.provider,
                version=run_agent.agent_version.version,
                total=run_agent.total,
                processed=processed,
                completed=run_agent.completed,
                failed=run_agent.failed,
                retried=run_agent.retried,
                progress_percent=(
                    round(processed * 100 / run_agent.total, 2) if run_agent.total else 0
                ),
                result=parsed_results.get(run_agent.agent_version_id),
                metric_scores=metric_scores[run_agent.agent_version_id],
                metric_ranks=metric_ranks[run_agent.agent_version_id],
            )
        )
    return EvaluationExperimentDetail(
        **summary_response(experiment).model_dump(),
        members=members,
        comparison_metrics=comparisons,
    )


@router.post(
    "",
    response_model=EvaluationExperimentDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_evaluation_experiment(
    payload: EvaluationExperimentCreate,
    request: Request,
    database: DatabaseSession,
    auth_session: OperatorCsrfSession,
) -> EvaluationExperimentDetail:
    creator_user_id, creator_subject_key, creator_display_name = creator_identity(auth_session)
    key = str(payload.idempotency_key)
    fingerprint = experiment_fingerprint(payload)
    async with sqlite_mutation_guard(
        request,
        database,
        f"evaluation-experiment-create:{creator_subject_key}:{key}",
    ):
        try:
            existing = await existing_experiment(
                database, creator_subject_key, key, fingerprint
            )
            if existing is not None:
                return detail_response(existing)

            profile = await resolve_profile(
                database, payload.profile_key, payload.dataset_version_ids
            )
            if (
                profile.score_config_id is None
                or payload.score_config_id != profile.score_config_id
            ):
                raise RunConfigurationError(
                    "profile_score_config_mismatch",
                    "评分配置必须与所选评测配置一致",
                )
            versions = list(
                await database.scalars(
                    select(AgentVersion)
                    .options(joinedload(AgentVersion.agent))
                    .where(AgentVersion.id.in_(payload.agent_version_ids))
                )
            )
            versions_by_id = {version.id: version for version in versions}
            if set(versions_by_id) != set(payload.agent_version_ids):
                raise RunConfigurationError(
                    "agent_version_not_found", "一个或多个 Agent 配置版本不存在"
                )
            if any(version.agent.owner_user_id is not None for version in versions):
                raise RunConfigurationError(
                    "private_agent_not_allowed", "私有 Agent 不能用于正式评测"
                )
            if len({version.agent_id for version in versions}) != len(versions):
                raise RunConfigurationError(
                    "duplicate_logical_agent", "同一实验只能选择同一 Agent 的一个配置版本"
                )
            await database.execute(
                select(AgentVersion.id)
                .where(AgentVersion.id.in_(payload.agent_version_ids))
                .order_by(AgentVersion.id)
                .with_for_update()
            )
            existing = await existing_experiment(
                database, creator_subject_key, key, fingerprint
            )
            if existing is not None:
                return detail_response(existing)

            prepared_runs = []
            for version_id in payload.agent_version_ids:
                version = versions_by_id[version_id]
                prepared_runs.append(
                    await prepare_evaluation_run(
                        database,
                        name=run_name_for_agent(payload.name, version.agent.name),
                        agent_version_ids=[version_id],
                        dataset_version_ids=list(profile.dataset_version_ids),
                        judge_config_id=payload.judge_config_id,
                        score_config_id=profile.score_config_id,
                        profile_key=profile.key,
                        run_scope=profile.scope,
                        target_ability=profile.target_ability,
                        ranking_eligible=profile.ranking_eligible,
                        prevent_duplicates=True,
                        force_retest=payload.force_retest,
                    )
                )
            protocol_hashes = {prepared.run.protocol_hash for prepared in prepared_runs}
            if len(protocol_hashes) != 1:
                raise RunConfigurationError(
                    "experiment_protocol_mismatch",
                    "实验中的所有评测任务必须使用同一协议",
                )
            experiment = EvaluationExperiment(
                name=payload.name,
                creator_user_id=creator_user_id,
                creator_subject_key=creator_subject_key,
                creator_display_name=creator_display_name,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                protocol_hash=next(iter(protocol_hashes)),
            )
            database.add(experiment)
            await database.flush()
            for position, prepared in enumerate(prepared_runs, start=1):
                prepared.run.experiment_id = experiment.id
                prepared.run.experiment_position = position
            await database.commit()
        except RunConfigurationError as error:
            await database.rollback()
            raise experiment_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT, error.code, str(error)
            ) from error
        except IntegrityError as error:
            await database.rollback()
            duplicate = await existing_experiment(
                database, creator_subject_key, key, fingerprint
            )
            if duplicate is not None:
                return detail_response(duplicate)
            raise experiment_error(
                status.HTTP_409_CONFLICT,
                "experiment_conflict",
                "Evaluation experiment changed concurrently",
            ) from error

        stored = await load_experiment(database, experiment.id)
        if stored is None:
            raise RuntimeError("created evaluation experiment is unavailable")
        return detail_response(stored)


@router.get("", response_model=list[EvaluationExperimentSummary])
async def list_evaluation_experiments(
    response: Response,
    database: DatabaseSession,
    _authorization: OperatorSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[EvaluationExperimentSummary]:
    experiments = list(
        await database.scalars(
            select(EvaluationExperiment)
            .options(*experiment_summary_options())
            .where(
                ~EvaluationExperiment.runs.any(
                    EvaluationRun.run_agents.any(
                        RunAgent.agent_version.has(
                            AgentVersion.agent.has(Agent.owner_user_id.is_not(None))
                        )
                    )
                )
            )
            .order_by(EvaluationExperiment.created_at.desc(), EvaluationExperiment.id.desc())
            .limit(limit)
        )
    )
    response.headers["Cache-Control"] = "private, no-store"
    return [summary_response(experiment) for experiment in experiments]


@router.get("/{experiment_id}", response_model=EvaluationExperimentDetail)
async def get_evaluation_experiment(
    experiment_id: UUID,
    response: Response,
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> EvaluationExperimentDetail:
    experiment = await load_experiment(database, experiment_id)
    if experiment is None:
        raise experiment_error(
            status.HTTP_404_NOT_FOUND,
            "experiment_not_found",
            "Evaluation experiment not found",
        )
    response.headers["Cache-Control"] = "private, no-store"
    return detail_response(experiment)
