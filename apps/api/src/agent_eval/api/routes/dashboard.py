from fastapi import APIRouter, Response
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from agent_eval.api.dependencies import CurrentSession, DatabaseSession
from agent_eval.api.schemas.dashboard import (
    DashboardActiveProfile,
    DashboardProfileSummary,
    DashboardSummary,
)
from agent_eval.db.enums import Ability, Language
from agent_eval.db.models import DatasetVersion, JudgeConfig
from agent_eval.runs.profiles import (
    RunProfile,
    available_run_profiles,
    build_run_profiles,
    default_profile_key,
)

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
async def get_dashboard_summary(
    response: Response,
    database: DatabaseSession,
    _authorization: CurrentSession,
) -> DashboardSummary:
    profiles = available_run_profiles(await build_run_profiles(database))
    active_key = default_profile_key(profiles)
    active_profile = next(profile for profile in profiles if profile.key == active_key)
    ranking_profiles = [profile for profile in profiles if profile.ranking_eligible]

    dataset_ids = {
        dataset_id for profile in ranking_profiles for dataset_id in profile.dataset_version_ids
    }
    datasets = (
        list(
            await database.scalars(
                select(DatasetVersion)
                .options(joinedload(DatasetVersion.benchmark))
                .where(DatasetVersion.id.in_(dataset_ids))
            )
        )
        if dataset_ids
        else []
    )
    datasets_by_id = {dataset.id: dataset for dataset in datasets}
    judge = await database.scalar(
        select(JudgeConfig)
        .where(JudgeConfig.enabled.is_(True))
        .order_by(JudgeConfig.provider, JudgeConfig.model, JudgeConfig.id)
    )

    def profile_ready(profile: RunProfile) -> bool:
        selected = [
            datasets_by_id[dataset_id]
            for dataset_id in profile.dataset_version_ids
            if dataset_id in datasets_by_id
        ]
        return (
            judge is not None
            and profile.score_config_id is not None
            and bool(selected)
            and profile.question_count > 0
            and len(selected) == len(profile.dataset_version_ids)
            and sum(dataset.question_count for dataset in selected) == profile.question_count
        )

    selected_datasets = [
        datasets_by_id[dataset_id]
        for dataset_id in active_profile.dataset_version_ids
        if dataset_id in datasets_by_id
    ]
    language_counts = {language: 0 for language in Language}
    ability_counts = {ability: 0 for ability in Ability}
    for dataset in selected_datasets:
        language_counts[dataset.benchmark.language] += dataset.question_count
        ability_counts[dataset.benchmark.ability] += dataset.question_count

    response.headers["Cache-Control"] = "private, no-store"
    return DashboardSummary(
        active_profile=DashboardActiveProfile(
            key=active_profile.key,
            name=active_profile.name,
            ready=profile_ready(active_profile),
            question_count=active_profile.question_count,
            dataset_version_count=len(active_profile.dataset_version_ids),
            language_counts=language_counts,
            ability_counts=ability_counts,
            ability_weights=active_profile.ability_weights,
        ),
        ranking_profiles=[
            DashboardProfileSummary(
                key=profile.key,
                name=profile.name,
                ready=profile_ready(profile),
                question_count=profile.question_count,
            )
            for profile in ranking_profiles
        ],
        judge_model=judge.model if judge is not None else None,
    )
