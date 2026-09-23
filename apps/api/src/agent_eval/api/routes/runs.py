from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.sql.elements import ColumnElement

from agent_eval.api.dependencies import DatabaseSession, OperatorCsrfSession, OperatorSession
from agent_eval.api.schemas.runs import (
    AgentEvaluationStatus,
    EvaluationRunBatchCreate,
    EvaluationRunBatchResponse,
    EvaluationRunCreate,
    EvaluationRunEvidenceList,
    EvaluationRunListItem,
    EvaluationRunOptions,
    EvaluationRunProgress,
    EvaluationRunResponse,
    EvaluationRunStateResponse,
    RunAgentOption,
    RunAgentProgress,
    RunAgentResultSummary,
    RunDatasetOption,
    RunEvidenceDetail,
    RunProfileCompatibility,
    RunProfileOption,
)
from agent_eval.db.enums import (
    Ability,
    AgentStatus,
    AgentVersionLifecycle,
    ExecutionStatus,
    Language,
    RunScope,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    DatasetVersion,
    EvaluationRun,
    ExecutionItem,
    JudgeConfig,
    LeaderboardSnapshot,
    Question,
    RunAgent,
    ScoreConfig,
)
from agent_eval.judges.deepseek import RUBRIC_PROMPT_VERSION
from agent_eval.leaderboards.service import get_agent_evaluation_statuses
from agent_eval.runs.compatibility import analyze_profile_compatibility
from agent_eval.runs.evidence import (
    RunEvidenceFilters,
    RunEvidenceNotFoundError,
    get_run_evidence_detail,
    list_run_evidence,
)
from agent_eval.runs.lifecycle import (
    RunStateError,
    request_run_cancellation,
    retry_failed_answer_scoring,
)
from agent_eval.runs.profiles import (
    RunProfile,
    available_run_profiles,
    build_run_profiles,
    default_profile_key,
)
from agent_eval.runs.service import PreparedRun, RunConfigurationError, prepare_evaluation_run

router = APIRouter(prefix="/api/v1", tags=["evaluation-runs"])


def public_run_filter() -> ColumnElement[bool]:
    return ~EvaluationRun.run_agents.any(
        RunAgent.agent_version.has(
            AgentVersion.agent.has(Agent.owner_user_id.is_not(None))
        )
    )


async def require_public_run(database: DatabaseSession, run_id: UUID) -> None:
    if await database.scalar(
        select(EvaluationRun.id).where(
            EvaluationRun.id == run_id,
            public_run_filter(),
        )
    ) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "run_not_found", "message": "评测任务不存在"},
        )


def state_response(run: EvaluationRun) -> EvaluationRunStateResponse:
    return EvaluationRunStateResponse(
        id=run.id,
        experiment_id=run.experiment_id,
        experiment_position=run.experiment_position,
        name=run.name,
        status=run.status,
        desired_state=run.desired_state,
        stage=run.stage,
        profile_key=run.profile_key,
        run_scope=run.run_scope,
        target_ability=run.target_ability,
        ranking_eligible=run.ranking_eligible,
        trial_count=run.trial_count,
        heartbeat_at=run.heartbeat_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        system_error=run.system_error,
        created_at=run.created_at,
    )


def list_response(run: EvaluationRun) -> EvaluationRunListItem:
    total = sum(agent.total for agent in run.run_agents)
    completed = sum(agent.completed for agent in run.run_agents)
    failed = sum(agent.failed for agent in run.run_agents)
    return EvaluationRunListItem(
        **state_response(run).model_dump(),
        agent_count=len(run.run_agents),
        total=total,
        completed=completed,
        failed=failed,
        retried=sum(agent.retried for agent in run.run_agents),
    )


def run_response(prepared: PreparedRun) -> EvaluationRunResponse:
    run = prepared.run
    return EvaluationRunResponse(
        id=run.id,
        experiment_id=run.experiment_id,
        experiment_position=run.experiment_position,
        name=run.name,
        status=run.status,
        desired_state=run.desired_state,
        stage=run.stage,
        dataset_bundle_hash=run.dataset_bundle_hash,
        profile_key=run.profile_key,
        run_scope=run.run_scope,
        target_ability=run.target_ability,
        ranking_eligible=run.ranking_eligible,
        trial_count=run.trial_count,
        protocol_hash=run.protocol_hash,
        judge_config_id=run.judge_config_id,
        score_config_id=run.score_config_id,
        agent_version_ids=prepared.agent_version_ids,
        dataset_version_ids=prepared.dataset_version_ids,
        question_count=prepared.question_count,
        execution_item_count=prepared.execution_item_count,
        created_at=run.created_at,
    )


def result_summary(value: object) -> RunAgentResultSummary | None:
    try:
        return RunAgentResultSummary.model_validate(value)
    except ValidationError:
        return None


async def resolve_profile(
    database: DatabaseSession,
    profile_key: str | None,
    dataset_version_ids: list[UUID] | None,
) -> RunProfile:
    profiles = {
        profile.key: profile
        for profile in available_run_profiles(await build_run_profiles(database))
    }
    selected_key = profile_key or default_profile_key(list(profiles.values()))
    profile = profiles.get(selected_key)
    if profile is None:
        raise RunConfigurationError("profile_not_found", "评测配置不存在或当前不可用")
    if dataset_version_ids is not None and set(dataset_version_ids) != set(
        profile.dataset_version_ids
    ):
        raise RunConfigurationError(
            "profile_dataset_mismatch", "题库版本必须与所选评测配置完全一致"
        )
    return profile


def run_name_for_agent(base_name: str, agent_name: str) -> str:
    suffix = f" · {agent_name}"
    return f"{base_name[: 160 - len(suffix)]}{suffix}"


def resolve_trial_count(requested: int | None, profile: RunProfile) -> int:
    if requested is not None:
        return requested
    return 5 if profile.ranking_eligible else 1


def compatibility_response(
    version: AgentVersion,
    profile: RunProfile,
    questions: list[Question],
) -> RunProfileCompatibility:
    compatibility = analyze_profile_compatibility(version, profile.key, questions)
    blocking_reasons = list(compatibility.blocking_reasons)
    if (
        version.lifecycle_status == AgentVersionLifecycle.VERIFIED
        and (profile.scope != RunScope.SMOKE or profile.ranking_eligible)
    ):
        blocking_reasons.append("候选版本只能运行非排名 Smoke")
    return RunProfileCompatibility(
        profile_key=compatibility.profile_key,
        compatible=not blocking_reasons,
        oversized_question_count=compatibility.oversized_question_count,
        max_question_characters=compatibility.max_question_characters,
        citation_required_count=compatibility.citation_required_count,
        blocking_reasons=blocking_reasons,
        warnings=list(compatibility.warnings),
    )


@router.get("/evaluation-runs/options", response_model=EvaluationRunOptions)
async def get_evaluation_run_options(
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> EvaluationRunOptions:
    versions = list(
        await database.scalars(
            select(AgentVersion)
            .options(joinedload(AgentVersion.agent))
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .where(
                AgentVersion.enabled.is_(True),
                AgentVersion.lifecycle_status.in_(
                    [AgentVersionLifecycle.ACTIVE, AgentVersionLifecycle.VERIFIED]
                ),
                Agent.status == AgentStatus.ENABLED,
                Agent.owner_user_id.is_(None),
            )
            .order_by(AgentVersion.agent_id, AgentVersion.version.desc())
        )
    )
    active_versions: dict[UUID, AgentVersion] = {}
    candidate_versions: dict[UUID, AgentVersion] = {}
    for agent_version in versions:
        if agent_version.lifecycle_status == AgentVersionLifecycle.ACTIVE:
            active_versions.setdefault(agent_version.agent_id, agent_version)
        else:
            candidate_versions.setdefault(agent_version.agent_id, agent_version)

    profiles = available_run_profiles(await build_run_profiles(database))
    all_dataset_ids = {
        dataset_id for profile in profiles for dataset_id in profile.dataset_version_ids
    }
    questions = list(
        await database.scalars(
            select(Question)
            .options(
                joinedload(Question.dataset_version).joinedload(DatasetVersion.benchmark)
            )
            .where(Question.dataset_version_id.in_(all_dataset_ids))
        )
    )
    questions_by_dataset: dict[UUID, list[Question]] = {}
    for question in questions:
        questions_by_dataset.setdefault(question.dataset_version_id, []).append(question)
    selected_profile = next(
        profile for profile in profiles if profile.key == default_profile_key(profiles)
    )
    profile_datasets = list(
        await database.scalars(
            select(DatasetVersion)
            .options(joinedload(DatasetVersion.benchmark))
            .where(DatasetVersion.id.in_(all_dataset_ids))
        )
    )
    profile_datasets.sort(
        key=lambda item: (item.benchmark.name.casefold(), str(item.id)),
    )
    datasets_by_id = {version.id: version for version in profile_datasets}
    selected_datasets = [
        datasets_by_id[dataset_id]
        for dataset_id in selected_profile.dataset_version_ids
        if dataset_id in datasets_by_id
    ]
    judge = await database.scalar(
        select(JudgeConfig)
        .where(JudgeConfig.enabled.is_(True))
        .order_by(
            JudgeConfig.prompt_version != RUBRIC_PROMPT_VERSION,
            JudgeConfig.provider,
            JudgeConfig.model,
            JudgeConfig.id,
        )
    )
    score = (
        await database.get(ScoreConfig, selected_profile.score_config_id)
        if selected_profile.score_config_id is not None
        else None
    )
    agents = sorted(
        [*active_versions.values(), *candidate_versions.values()],
        key=lambda item: (
            item.agent.name.casefold(),
            item.lifecycle_status != AgentVersionLifecycle.ACTIVE,
            -item.version,
        ),
    )
    issues: list[str] = []
    if not agents:
        issues.append("至少需要一个已启用且已配置的 Agent")
    if not selected_datasets:
        issues.append("没有可用的冻结题库")
    if judge is None:
        issues.append("没有可用的裁判配置")
    if score is None:
        issues.append("默认评测配置没有可用的评分配置")
    return EvaluationRunOptions(
        agents=[
            RunAgentOption(
                agent_id=version.agent_id,
                agent_version_id=version.id,
                name=version.agent.name,
                provider=version.agent.provider,
                version=version.version,
                lifecycle_status=version.lifecycle_status,
                max_prompt_characters=version.max_prompt_characters,
                supports_citations=version.citations_jsonpath is not None,
                profile_compatibility=[
                    compatibility_response(
                        version,
                        profile,
                        [
                            question
                            for dataset_id in profile.dataset_version_ids
                            for question in questions_by_dataset.get(dataset_id, [])
                        ],
                    )
                    for profile in profiles
                ],
            )
            for version in agents
        ],
        profiles=[
            RunProfileOption(
                key=profile.key,
                name=profile.name,
                description=profile.description,
                scope=profile.scope,
                target_ability=profile.target_ability,
                dataset_version_ids=list(profile.dataset_version_ids),
                datasets=[
                    RunDatasetOption(
                        benchmark_id=version.benchmark_id,
                        dataset_version_id=version.id,
                        name=version.benchmark.name,
                        ability=version.benchmark.ability,
                        language=version.benchmark.language,
                        version=version.version,
                        question_count=version.question_count,
                        cohort_key=version.cohort_key,
                    )
                    for dataset_id in profile.dataset_version_ids
                    if (version := datasets_by_id.get(dataset_id)) is not None
                ],
                question_count=profile.question_count,
                benchmark_count=profile.benchmark_count,
                publishes_to=list(profile.publishes_to),
                ranking_eligible=profile.ranking_eligible,
                score_config_id=profile.score_config_id,
                ability_weights=profile.ability_weights,
            )
            for profile in profiles
        ],
        default_profile_key=selected_profile.key,
        datasets=[
            RunDatasetOption(
                benchmark_id=version.benchmark_id,
                dataset_version_id=version.id,
                name=version.benchmark.name,
                ability=version.benchmark.ability,
                language=version.benchmark.language,
                version=version.version,
                question_count=version.question_count,
                cohort_key=version.cohort_key,
            )
            for version in selected_datasets
        ],
        dataset_version_ids=[version.id for version in selected_datasets],
        question_count=sum(version.question_count for version in selected_datasets),
        judge_config_id=judge.id if judge is not None else None,
        judge_model=judge.model if judge is not None else None,
        score_config_id=score.id if score is not None else None,
        ability_weights={
            Ability.REASONING: score.reasoning_weight if score is not None else 40,
            Ability.RETRIEVAL: score.retrieval_weight if score is not None else 35,
            Ability.DATA_REASONING: score.data_weight if score is not None else 25,
            Ability.INSTRUCTION_FOLLOWING: (
                score.instruction_following_weight if score is not None else 0
            ),
            Ability.LONG_CONTEXT: score.long_context_weight if score is not None else 0,
        },
        ready=not issues,
        issues=issues,
    )


@router.get("/evaluation-runs", response_model=list[EvaluationRunListItem])
async def list_evaluation_runs(
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> list[EvaluationRunListItem]:
    runs = list(
        await database.scalars(
            select(EvaluationRun)
            .options(selectinload(EvaluationRun.run_agents))
            .where(public_run_filter())
            .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
        )
    )
    return [list_response(run) for run in runs]


@router.post(
    "/evaluation-runs",
    response_model=EvaluationRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_evaluation_run(
    payload: EvaluationRunCreate,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> EvaluationRunResponse:
    try:
        profile = await resolve_profile(database, payload.profile_key, payload.dataset_version_ids)
        if payload.score_config_id != profile.score_config_id:
            raise RunConfigurationError(
                "profile_score_config_mismatch", "评分配置必须与所选评测配置一致"
            )
        prepared = await prepare_evaluation_run(
            database,
            name=payload.name,
            agent_version_ids=payload.agent_version_ids,
            dataset_version_ids=list(profile.dataset_version_ids),
            judge_config_id=payload.judge_config_id,
            score_config_id=profile.score_config_id,
            profile_key=profile.key,
            run_scope=profile.scope,
            target_ability=profile.target_ability,
            ranking_eligible=profile.ranking_eligible,
            trial_count=resolve_trial_count(payload.trial_count, profile),
            prevent_duplicates=True,
            force_retest=payload.force_retest,
        )
        await database.commit()
    except RunConfigurationError as error:
        await database.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": error.code, "message": str(error)},
        ) from error

    return run_response(prepared)


@router.post(
    "/evaluation-runs/batch",
    response_model=EvaluationRunBatchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_evaluation_run_batch(
    payload: EvaluationRunBatchCreate,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> EvaluationRunBatchResponse:
    try:
        profile = await resolve_profile(database, payload.profile_key, payload.dataset_version_ids)
        if payload.score_config_id != profile.score_config_id:
            raise RunConfigurationError(
                "profile_score_config_mismatch", "评分配置必须与所选评测配置一致"
            )
        versions = list(
            await database.scalars(
                select(AgentVersion)
                .options(joinedload(AgentVersion.agent))
                .where(AgentVersion.id.in_(payload.agent_version_ids))
            )
        )
        versions_by_id = {version.id: version for version in versions}
        await database.execute(
            select(AgentVersion.id)
            .where(AgentVersion.id.in_(payload.agent_version_ids))
            .order_by(AgentVersion.id)
            .with_for_update()
        )
        prepared_runs = []
        for version_id in payload.agent_version_ids:
            version = versions_by_id.get(version_id)
            if version is None:
                raise RunConfigurationError(
                    "agent_version_not_found", "一个或多个 Agent 配置版本不存在"
                )
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
                    trial_count=resolve_trial_count(payload.trial_count, profile),
                    prevent_duplicates=True,
                    force_retest=payload.force_retest,
                )
            )
        await database.commit()
    except RunConfigurationError as error:
        await database.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": error.code, "message": str(error)},
        ) from error
    return EvaluationRunBatchResponse(runs=[run_response(item) for item in prepared_runs])


@router.get("/evaluation-runs/agent-statuses", response_model=list[AgentEvaluationStatus])
async def get_evaluation_run_agent_statuses(
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> list[AgentEvaluationStatus]:
    return await get_agent_evaluation_statuses(database)


@router.get("/evaluation-runs/{run_id}", response_model=EvaluationRunStateResponse)
async def get_evaluation_run(
    run_id: UUID,
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> EvaluationRunStateResponse:
    run = await database.scalar(
        select(EvaluationRun).where(
            EvaluationRun.id == run_id,
            public_run_filter(),
        )
    )
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "run_not_found", "message": "评测任务不存在"},
        )
    return state_response(run)


@router.get("/evaluation-runs/{run_id}/progress", response_model=EvaluationRunProgress)
async def get_evaluation_run_progress(
    run_id: UUID,
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> EvaluationRunProgress:
    run = await database.scalar(
        select(EvaluationRun)
        .options(
            selectinload(EvaluationRun.run_agents)
            .joinedload(RunAgent.agent_version)
            .joinedload(AgentVersion.agent)
        )
        .where(EvaluationRun.id == run_id, public_run_filter())
    )
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "run_not_found", "message": "评测任务不存在"},
        )
    failure_rows = (
        await database.execute(
            select(
                ExecutionItem.run_agent_id,
                ExecutionItem.error_type,
                func.count(),
            )
            .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
            .where(
                RunAgent.run_id == run_id,
                ExecutionItem.status == ExecutionStatus.FAILED,
                ExecutionItem.error_type.is_not(None),
            )
            .group_by(ExecutionItem.run_agent_id, ExecutionItem.error_type)
        )
    ).all()
    failures_by_agent: dict[UUID, dict[str, int]] = {}
    for run_agent_id, error_type, count in failure_rows:
        if error_type is not None:
            failures_by_agent.setdefault(run_agent_id, {})[error_type] = count
    snapshot = await database.scalar(
        select(LeaderboardSnapshot)
        .where(LeaderboardSnapshot.run_id == run_id)
        .order_by(LeaderboardSnapshot.published_at.desc(), LeaderboardSnapshot.id.desc())
        .limit(1)
    )
    results_by_version: dict[str, RunAgentResultSummary] = {}
    for item in snapshot.rankings_json if snapshot is not None else []:
        if not isinstance(item, dict) or not isinstance(item.get("agent_version_id"), str):
            continue
        if (summary := result_summary(item)) is not None:
            results_by_version[item["agent_version_id"]] = summary
    agents = [
        RunAgentProgress(
            run_agent_id=run_agent.id,
            agent_id=run_agent.agent_version.agent_id,
            agent_version_id=run_agent.agent_version_id,
            agent_name=run_agent.agent_version.agent.name,
            provider=run_agent.agent_version.agent.provider,
            version=run_agent.agent_version.version,
            total=run_agent.total,
            completed=run_agent.completed,
            failed=run_agent.failed,
            retried=run_agent.retried,
            processed=run_agent.completed + run_agent.failed,
            failure_categories=failures_by_agent.get(run_agent.id, {}),
            validity_status=run_agent.validity_status,
            validity_reason=run_agent.validity_reason,
            configuration_failure_count=run_agent.configuration_failure_count,
            configuration_failure_rate=float(run_agent.configuration_failure_rate),
            result=results_by_version.get(str(run_agent.agent_version_id)),
        )
        for run_agent in sorted(
            run.run_agents,
            key=lambda item: item.agent_version.agent.name.casefold(),
        )
    ]
    total = sum(agent.total for agent in agents)
    completed = sum(agent.completed for agent in agents)
    failed = sum(agent.failed for agent in agents)
    processed = completed + failed
    return EvaluationRunProgress(
        **state_response(run).model_dump(),
        total=total,
        completed=completed,
        failed=failed,
        retried=sum(agent.retried for agent in agents),
        processed=processed,
        progress_percent=round(processed * 100 / total, 2) if total else 0,
        agents=agents,
    )


@router.get("/evaluation-runs/{run_id}/evidence", response_model=EvaluationRunEvidenceList)
async def get_evaluation_run_evidence(
    run_id: UUID,
    database: DatabaseSession,
    _authorization: OperatorSession,
    response: Response,
    run_agent_id: UUID | None = None,
    item_status: Annotated[ExecutionStatus | None, Query(alias="status")] = None,
    ability: Ability | None = None,
    language: Language | None = None,
    error_type: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
    min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
    max_score: Annotated[float | None, Query(ge=0, le=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EvaluationRunEvidenceList:
    response.headers["Cache-Control"] = "private, no-store"
    await require_public_run(database, run_id)
    if min_score is not None and max_score is not None and min_score > max_score:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_score_range", "message": "最低分不能高于最高分"},
        )
    try:
        return await list_run_evidence(
            database,
            run_id,
            filters=RunEvidenceFilters(
                run_agent_id=run_agent_id,
                status=item_status,
                ability=ability,
                language=language,
                error_type=error_type,
                min_score=min_score,
                max_score=max_score,
            ),
            limit=limit,
            offset=offset,
        )
    except RunEvidenceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "run_not_found", "message": "评测任务不存在"},
        ) from error


@router.get(
    "/evaluation-runs/{run_id}/evidence/{execution_item_id}",
    response_model=RunEvidenceDetail,
)
async def get_evaluation_run_evidence_item(
    run_id: UUID,
    execution_item_id: UUID,
    request: Request,
    response: Response,
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> RunEvidenceDetail:
    response.headers["Cache-Control"] = "private, no-store"
    await require_public_run(database, run_id)
    try:
        return await get_run_evidence_detail(
            database,
            request.app.state.artifact_store,
            run_id,
            execution_item_id,
        )
    except RunEvidenceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "evidence_not_found", "message": "评测题目证据不存在"},
        ) from error


@router.post("/evaluation-runs/{run_id}/cancel", response_model=EvaluationRunStateResponse)
async def cancel_evaluation_run(
    run_id: UUID,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> EvaluationRunStateResponse:
    await require_public_run(database, run_id)
    try:
        run = await request_run_cancellation(database, run_id)
        await database.commit()
        await database.refresh(run)
    except RunStateError as error:
        await database.rollback()
        status_code = (
            status.HTTP_404_NOT_FOUND if error.code == "run_not_found" else status.HTTP_409_CONFLICT
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": error.code, "message": str(error)},
        ) from error
    return state_response(run)


@router.post(
    "/evaluation-runs/{run_id}/retry-scoring",
    response_model=EvaluationRunStateResponse,
)
async def retry_evaluation_run_scoring(
    run_id: UUID,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> EvaluationRunStateResponse:
    await require_public_run(database, run_id)
    try:
        run = await retry_failed_answer_scoring(database, run_id)
        await database.commit()
        await database.refresh(run)
    except RunStateError as error:
        await database.rollback()
        status_code = (
            status.HTTP_404_NOT_FOUND if error.code == "run_not_found" else status.HTTP_409_CONFLICT
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": error.code, "message": str(error)},
        ) from error
    return state_response(run)
