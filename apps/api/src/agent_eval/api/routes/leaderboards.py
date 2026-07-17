from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from agent_eval.api.dependencies import CurrentSession, DatabaseSession
from agent_eval.api.schemas.leaderboards import (
    AgentRanking,
    GlobalLeaderboardOverviewResponse,
    GlobalLeaderboardResponse,
    LeaderboardHistoryItem,
    LeaderboardMetric,
    LeaderboardSnapshotResponse,
)
from agent_eval.db.enums import ResultValidityStatus, RunScope
from agent_eval.db.models import EvaluationRun, LeaderboardSnapshot, RunAgent
from agent_eval.leaderboards.service import (
    get_global_leaderboard,
    get_global_leaderboard_overview,
)
from agent_eval.runs.profiles import UNIFIED_V3_PROFILE_KEY

router = APIRouter(prefix="/api/v1/leaderboards", tags=["leaderboards"])


def snapshot_response(
    snapshot: LeaderboardSnapshot,
    *,
    rankings: list[dict[str, object]] | None = None,
) -> LeaderboardSnapshotResponse:
    return LeaderboardSnapshotResponse(
        id=snapshot.id,
        run_id=snapshot.run_id,
        run_name=snapshot.run.name,
        dataset_bundle_hash=snapshot.run.dataset_bundle_hash,
        published_at=snapshot.published_at,
        rankings=[
            AgentRanking.model_validate(item)
            for item in (snapshot.rankings_json if rankings is None else rankings)
        ],
    )


async def load_snapshot(database: DatabaseSession, snapshot_id: UUID) -> LeaderboardSnapshot:
    snapshot = await database.scalar(
        select(LeaderboardSnapshot)
        .options(joinedload(LeaderboardSnapshot.run))
        .where(LeaderboardSnapshot.id == snapshot_id)
    )
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "leaderboard_not_found", "message": "榜单快照不存在"},
        )
    return snapshot


@router.get("/current", response_model=LeaderboardSnapshotResponse)
async def get_current_leaderboard(
    database: DatabaseSession,
    _authorization: CurrentSession,
) -> LeaderboardSnapshotResponse:
    snapshot = await database.scalar(
        select(LeaderboardSnapshot)
        .options(joinedload(LeaderboardSnapshot.run))
        .join(EvaluationRun, EvaluationRun.id == LeaderboardSnapshot.run_id)
        .where(LeaderboardSnapshot.is_current.is_(True))
        .where(EvaluationRun.profile_key == UNIFIED_V3_PROFILE_KEY)
        .where(EvaluationRun.ranking_eligible.is_(True))
        .where(EvaluationRun.run_scope == RunScope.COMPREHENSIVE)
        .where(
            LeaderboardSnapshot.run_id.in_(
                select(RunAgent.run_id).where(
                    RunAgent.validity_status == ResultValidityStatus.VALID
                )
            )
        )
    )
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "leaderboard_not_found", "message": "当前尚无已发布榜单"},
        )
    return snapshot_response(snapshot)


@router.get("/history", response_model=list[LeaderboardHistoryItem])
async def get_leaderboard_history(
    database: DatabaseSession,
    _authorization: CurrentSession,
) -> list[LeaderboardHistoryItem]:
    snapshots = list(
        await database.scalars(
            select(LeaderboardSnapshot)
            .options(joinedload(LeaderboardSnapshot.run))
            .order_by(LeaderboardSnapshot.published_at.desc(), LeaderboardSnapshot.id.desc())
        )
    )
    return [
        LeaderboardHistoryItem(
            id=snapshot.id,
            run_id=snapshot.run_id,
            run_name=snapshot.run.name,
            published_at=snapshot.published_at,
            agent_count=len(snapshot.rankings_json),
            is_current=snapshot.is_current,
        )
        for snapshot in snapshots
    ]


@router.get("/global/overview", response_model=GlobalLeaderboardOverviewResponse)
async def get_global_leaderboard_overview_route(
    database: DatabaseSession,
    _authorization: CurrentSession,
) -> GlobalLeaderboardOverviewResponse:
    return await get_global_leaderboard_overview(database)


@router.get("/global/compare", response_model=GlobalLeaderboardOverviewResponse)
async def compare_global_agents(
    agent_ids: Annotated[list[UUID], Query(min_length=2, max_length=5)],
    database: DatabaseSession,
    _authorization: CurrentSession,
) -> GlobalLeaderboardOverviewResponse:
    if len(agent_ids) != len(set(agent_ids)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "duplicate_agents", "message": "对比 Agent 不能重复"},
        )
    overview = await get_global_leaderboard_overview(database)
    selected_ids = set(agent_ids)
    selected = [agent for agent in overview.agents if agent.agent_id in selected_ids]
    if len(selected) != len(agent_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "agents_not_available", "message": "一个或多个 Agent 不可用于对比"},
        )
    return overview.model_copy(
        update={"eligible_agent_count": len(selected), "agents": selected}
    )


@router.get("/global/{metric}", response_model=GlobalLeaderboardResponse)
async def get_global_leaderboard_route(
    metric: LeaderboardMetric,
    database: DatabaseSession,
    _authorization: CurrentSession,
) -> GlobalLeaderboardResponse:
    return await get_global_leaderboard(database, metric)


@router.get("/{snapshot_id}/compare", response_model=LeaderboardSnapshotResponse)
async def compare_snapshot_agents(
    snapshot_id: UUID,
    agent_version_ids: Annotated[list[UUID], Query(min_length=2, max_length=5)],
    database: DatabaseSession,
    _authorization: CurrentSession,
) -> LeaderboardSnapshotResponse:
    if len(agent_version_ids) != len(set(agent_version_ids)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "duplicate_agents", "message": "对比 Agent 不能重复"},
        )
    snapshot = await load_snapshot(database, snapshot_id)
    selected = [
        item
        for item in snapshot.rankings_json
        if UUID(str(item["agent_version_id"])) in set(agent_version_ids)
    ]
    if len(selected) != len(agent_version_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "agents_not_in_snapshot", "message": "一个或多个 Agent 不在该榜单中"},
        )
    return snapshot_response(snapshot, rankings=selected)


@router.get("/{snapshot_id}", response_model=LeaderboardSnapshotResponse)
async def get_leaderboard_snapshot(
    snapshot_id: UUID,
    database: DatabaseSession,
    _authorization: CurrentSession,
) -> LeaderboardSnapshotResponse:
    return snapshot_response(await load_snapshot(database, snapshot_id))
