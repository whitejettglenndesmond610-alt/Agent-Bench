from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.sql import Select

from agent_eval.api.dependencies import CurrentSession, DatabaseSession
from agent_eval.api.schemas.leaderboards import (
    AgentLeaderboardDetailResponse,
    AgentRanking,
    GlobalLeaderboardOverviewResponse,
    GlobalLeaderboardResponse,
    LeaderboardHistoryItem,
    LeaderboardMetric,
    LeaderboardSnapshotResponse,
)
from agent_eval.db.enums import ResultValidityStatus, RunScope, RunStatus
from agent_eval.db.models import Agent, AgentVersion, EvaluationRun, LeaderboardSnapshot, RunAgent
from agent_eval.leaderboards.service import (
    get_agent_leaderboard_detail,
    get_global_leaderboard,
    get_global_leaderboard_overview,
)
from agent_eval.runs.profiles import UNIFIED_V3_PROFILE_KEY

router = APIRouter(prefix="/api/v1/leaderboards", tags=["leaderboards"])


def valid_public_run_ids() -> Select[tuple[UUID]]:
    return (
        select(RunAgent.run_id)
        .join(AgentVersion, AgentVersion.id == RunAgent.agent_version_id)
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .where(
            RunAgent.validity_status == ResultValidityStatus.VALID,
            Agent.owner_user_id.is_(None),
        )
    )


def private_run_ids() -> Select[tuple[UUID]]:
    return (
        select(RunAgent.run_id)
        .join(AgentVersion, AgentVersion.id == RunAgent.agent_version_id)
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .where(Agent.owner_user_id.is_not(None))
    )


def snapshot_response(
    snapshot: LeaderboardSnapshot,
    *,
    rankings: list[dict[str, object]] | None = None,
) -> LeaderboardSnapshotResponse:
    valid_version_ids = {
        str(run_agent.agent_version_id)
        for run_agent in snapshot.run.run_agents
        if run_agent.validity_status == ResultValidityStatus.VALID
    }
    source_rankings = snapshot.rankings_json if rankings is None else rankings
    public_rankings = [
        item
        for item in source_rankings
        if str(item.get("agent_version_id")) in valid_version_ids
    ]
    return LeaderboardSnapshotResponse(
        id=snapshot.id,
        run_id=snapshot.run_id,
        run_name=snapshot.run.name,
        dataset_bundle_hash=snapshot.run.dataset_bundle_hash,
        published_at=snapshot.published_at,
        rankings=[
            AgentRanking.model_validate(item)
            for item in public_rankings
        ],
    )


async def load_snapshot(database: DatabaseSession, snapshot_id: UUID) -> LeaderboardSnapshot:
    snapshot = await database.scalar(
        select(LeaderboardSnapshot)
        .options(
            joinedload(LeaderboardSnapshot.run).selectinload(EvaluationRun.run_agents)
        )
        .join(EvaluationRun, EvaluationRun.id == LeaderboardSnapshot.run_id)
        .where(
            LeaderboardSnapshot.id == snapshot_id,
            EvaluationRun.status == RunStatus.PUBLISHED,
            EvaluationRun.ranking_eligible.is_(True),
            LeaderboardSnapshot.run_id.in_(
                valid_public_run_ids()
            ),
            LeaderboardSnapshot.run_id.not_in(private_run_ids()),
        )
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
        .options(
            joinedload(LeaderboardSnapshot.run).selectinload(EvaluationRun.run_agents)
        )
        .join(EvaluationRun, EvaluationRun.id == LeaderboardSnapshot.run_id)
        .where(LeaderboardSnapshot.is_current.is_(True))
        .where(EvaluationRun.profile_key == UNIFIED_V3_PROFILE_KEY)
        .where(EvaluationRun.ranking_eligible.is_(True))
        .where(EvaluationRun.run_scope == RunScope.COMPREHENSIVE)
        .where(EvaluationRun.status == RunStatus.PUBLISHED)
        .where(
            LeaderboardSnapshot.run_id.in_(valid_public_run_ids()),
            LeaderboardSnapshot.run_id.not_in(private_run_ids()),
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
            .options(
                joinedload(LeaderboardSnapshot.run).selectinload(EvaluationRun.run_agents)
            )
            .join(EvaluationRun, EvaluationRun.id == LeaderboardSnapshot.run_id)
            .where(
                EvaluationRun.status == RunStatus.PUBLISHED,
                EvaluationRun.ranking_eligible.is_(True),
                LeaderboardSnapshot.run_id.in_(
                    valid_public_run_ids()
                ),
                LeaderboardSnapshot.run_id.not_in(private_run_ids()),
            )
            .order_by(LeaderboardSnapshot.published_at.desc(), LeaderboardSnapshot.id.desc())
        )
    )
    return [
        LeaderboardHistoryItem(
            id=snapshot.id,
            run_id=snapshot.run_id,
            run_name=snapshot.run.name,
            published_at=snapshot.published_at,
            agent_count=sum(
                1
                for item in snapshot.rankings_json
                if str(item.get("agent_version_id"))
                in {
                    str(run_agent.agent_version_id)
                    for run_agent in snapshot.run.run_agents
                    if run_agent.validity_status == ResultValidityStatus.VALID
                }
            ),
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


@router.get("/agents/{agent_id}", response_model=AgentLeaderboardDetailResponse)
async def get_agent_leaderboard_detail_route(
    agent_id: UUID,
    database: DatabaseSession,
    _authorization: CurrentSession,
    history_limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AgentLeaderboardDetailResponse:
    detail = await get_agent_leaderboard_detail(
        database,
        agent_id,
        history_limit=history_limit,
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "agent_not_available", "message": "Agent 不存在或不可用"},
        )
    return detail


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
    valid_version_ids = {
        run_agent.agent_version_id
        for run_agent in snapshot.run.run_agents
        if run_agent.validity_status == ResultValidityStatus.VALID
    }
    selected = [
        item
        for item in snapshot.rankings_json
        if UUID(str(item["agent_version_id"])) in set(agent_version_ids)
        and UUID(str(item["agent_version_id"])) in valid_version_ids
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
