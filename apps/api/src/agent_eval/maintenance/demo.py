import asyncio
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    AgentStatus,
    AgentVersionLifecycle,
    ArtifactKind,
    AuthType,
    CitationFetchStatus,
    ExecutionStatus,
    RunStatus,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    ArtifactIndex,
    EvaluationRun,
    ExecutionItem,
    JudgeConfig,
    LeaderboardSnapshot,
    RunAgent,
    ScoreConfig,
)
from agent_eval.maintenance.bootstrap import DEFAULT_JUDGE, DEFAULT_SCORE_NAME
from agent_eval.runs.profiles import build_run_profiles
from agent_eval.runs.service import prepare_evaluation_run

FAKE_AGENTS = (
    {
        "name": "Fake Agent Alpha",
        "provider": "Local Demo",
        "endpoint_url": "http://fake-agent-alpha:9000/run",
        "answer_jsonpath": "$.result.answer",
        "citations_jsonpath": "$.result.sources",
    },
    {
        "name": "Fake Agent Beta",
        "provider": "Local Demo",
        "endpoint_url": "http://fake-agent-beta:9000/run",
        "answer_jsonpath": "$.answer",
        "citations_jsonpath": "$.citations",
    },
)
TERMINAL_RUN_STATUSES = {
    RunStatus.PUBLISHED,
    RunStatus.COMPLETED,
    RunStatus.CANCELED,
    RunStatus.SYSTEM_FAILED,
}


async def seed_fake_agents(session: AsyncSession) -> list[UUID]:
    version_ids: list[UUID] = []
    for config in FAKE_AGENTS:
        agent = await session.scalar(select(Agent).where(Agent.name == config["name"]))
        if agent is None:
            agent = Agent(
                name=config["name"],
                provider=config["provider"],
                description="Deterministic local-only demo Agent",
                status=AgentStatus.ENABLED,
            )
            session.add(agent)
            await session.flush()
        elif agent.provider != config["provider"]:
            raise RuntimeError(f"existing Agent has unexpected provider: {agent.name}")
        else:
            agent.status = AgentStatus.ENABLED
        version = await session.scalar(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent.id,
                AgentVersion.version == 1,
            )
        )
        if version is None:
            version = AgentVersion(
                agent_id=agent.id,
                version=1,
                endpoint_url=config["endpoint_url"],
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath=config["answer_jsonpath"],
                citations_jsonpath=config["citations_jsonpath"],
                timeout_seconds=30,
                concurrency=5,
                enabled=True,
            )
            session.add(version)
            await session.flush()
        elif (
            version.endpoint_url != config["endpoint_url"]
            or version.answer_jsonpath != config["answer_jsonpath"]
            or version.citations_jsonpath != config["citations_jsonpath"]
        ):
            raise RuntimeError(f"existing fake Agent version has unexpected config: {agent.name}")
        version.enabled = True
        version.lifecycle_status = AgentVersionLifecycle.ACTIVE
        version_ids.append(version.id)
    return version_ids


async def queue_fake_run(
    session: AsyncSession,
    *,
    question_count: int,
) -> UUID:
    version_ids = await seed_fake_agents(session)
    run_id = await _queue_run(
        session,
        version_ids=version_ids,
        question_count=question_count,
        name_prefix="Local Fake",
        ranking_eligible=False,
    )
    await session.execute(
        update(Agent)
        .where(Agent.name.in_([config["name"] for config in FAKE_AGENTS]))
        .values(status=AgentStatus.DISABLED)
    )
    return run_id


async def queue_real_run(
    session: AsyncSession,
    *,
    question_count: int,
) -> UUID:
    return await queue_agent_pair_run(
        session,
        agent_names=("Qwen Plus", "Qwen Max"),
        question_count=question_count,
    )


async def queue_agent_pair_run(
    session: AsyncSession,
    *,
    agent_names: tuple[str, str],
    question_count: int,
) -> UUID:
    if len(set(agent_names)) != 2:
        raise ValueError("exactly two distinct Agent names are required")
    version_ids: list[UUID] = []
    for agent_name in agent_names:
        version_id = await session.scalar(
            select(AgentVersion.id)
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .where(
                Agent.name == agent_name,
                Agent.status == AgentStatus.ENABLED,
                AgentVersion.enabled.is_(True),
                AgentVersion.lifecycle_status == AgentVersionLifecycle.ACTIVE,
            )
            .order_by(AgentVersion.version.desc())
            .limit(1)
        )
        if version_id is None:
            raise RuntimeError(f"enabled Agent version not found: {agent_name}")
        version_ids.append(version_id)
    return await _queue_run(
        session,
        version_ids=version_ids,
        question_count=question_count,
        name_prefix=f"{agent_names[0]} vs {agent_names[1]}",
    )


async def _queue_run(
    session: AsyncSession,
    *,
    version_ids: list[UUID],
    question_count: int,
    name_prefix: str,
    ranking_eligible: bool | None = None,
) -> UUID:
    if question_count not in {20, 100}:
        raise ValueError("run supports exactly 20 or 100 questions")
    provider, model, prompt_version = DEFAULT_JUDGE
    judge_id = await session.scalar(
        select(JudgeConfig.id).where(
            JudgeConfig.provider == provider,
            JudgeConfig.model == model,
            JudgeConfig.prompt_version == prompt_version,
            JudgeConfig.enabled.is_(True),
        )
    )
    score_id = await session.scalar(
        select(ScoreConfig.id).where(ScoreConfig.name == DEFAULT_SCORE_NAME)
    )
    if judge_id is None or score_id is None:
        raise RuntimeError("run bootstrap before queuing a fake evaluation")

    profile_key = "smoke" if question_count == 20 else "comprehensive"
    profiles = {profile.key: profile for profile in await build_run_profiles(session)}
    profile = profiles.get(profile_key)
    if profile is None or profile.question_count != question_count:
        raise RuntimeError(f"could not select a {question_count}-question frozen dataset bundle")

    prepared = await prepare_evaluation_run(
        session,
        name=f"{name_prefix} {question_count}Q {utc_now():%Y-%m-%d %H:%M:%S}",
        agent_version_ids=version_ids,
        dataset_version_ids=list(profile.dataset_version_ids),
        judge_config_id=judge_id,
        score_config_id=score_id,
        profile_key=profile.key,
        run_scope=profile.scope,
        target_ability=profile.target_ability,
        ranking_eligible=(
            profile.ranking_eligible if ranking_eligible is None else ranking_eligible
        ),
    )
    return prepared.run.id


async def wait_for_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    *,
    timeout_seconds: int,
) -> RunStatus:
    deadline = utc_now() + timedelta(seconds=timeout_seconds)
    while utc_now() < deadline:
        async with session_factory() as session:
            status = await session.scalar(
                select(EvaluationRun.status).where(EvaluationRun.id == run_id)
            )
        if status is None:
            raise RuntimeError("evaluation run does not exist")
        if status in TERMINAL_RUN_STATUSES:
            return status
        await asyncio.sleep(1)
    raise TimeoutError(f"evaluation run did not finish within {timeout_seconds} seconds")


async def build_run_report(session: AsyncSession, run_id: UUID) -> dict[str, object]:
    run = await session.scalar(
        select(EvaluationRun)
        .options(
            selectinload(EvaluationRun.run_agents)
            .joinedload(RunAgent.agent_version)
            .joinedload(AgentVersion.agent),
            selectinload(EvaluationRun.run_agents)
            .selectinload(RunAgent.execution_items)
            .selectinload(ExecutionItem.attempts),
            selectinload(EvaluationRun.run_agents)
            .selectinload(RunAgent.execution_items)
            .selectinload(ExecutionItem.citations),
        )
        .where(EvaluationRun.id == run_id)
    )
    if run is None:
        raise RuntimeError("evaluation run does not exist")
    items = [item for run_agent in run.run_agents for item in run_agent.execution_items]
    citations = [citation for item in items for citation in item.citations]
    unreachable_statuses = {
        CitationFetchStatus.BLOCKED,
        CitationFetchStatus.UNREACHABLE,
        CitationFetchStatus.UNSUPPORTED,
    }
    unreachable_count = sum(
        citation.fetch_status in unreachable_statuses for citation in citations
    )
    judge_outputs = await session.scalar(
        select(func.count())
        .select_from(ArtifactIndex)
        .where(
            ArtifactIndex.kind == ArtifactKind.JUDGE_RESPONSE,
            ArtifactIndex.relative_path.like(f"runs/{run_id}/%"),
        )
    )
    artifact_bytes = await session.scalar(
        select(func.coalesce(func.sum(ArtifactIndex.size_bytes), 0)).where(
            ArtifactIndex.relative_path.like(f"runs/{run_id}/%")
        )
    )
    snapshot_id = await session.scalar(
        select(LeaderboardSnapshot.id).where(LeaderboardSnapshot.run_id == run_id)
    )
    duration_seconds: float | None = None
    if run.started_at is not None and run.finished_at is not None:
        duration_seconds = round((run.finished_at - run.started_at).total_seconds(), 3)
    failed_count = sum(item.status == ExecutionStatus.FAILED for item in items)
    failures_by_error: dict[str, int] = {}
    per_agent: list[dict[str, object]] = []
    for run_agent in run.run_agents:
        agent_failed = sum(
            item.status == ExecutionStatus.FAILED for item in run_agent.execution_items
        )
        status_counts: dict[str, int] = {}
        for item in run_agent.execution_items:
            for attempt in item.attempts:
                status_key = str(attempt.status_code) if attempt.status_code is not None else "none"
                status_counts[status_key] = status_counts.get(status_key, 0) + 1
        per_agent.append(
            {
                "agent_name": run_agent.agent_version.agent.name,
                "version": run_agent.agent_version.version,
                "failed_item_count": agent_failed,
                "http_call_count": sum(
                    len(item.attempts) for item in run_agent.execution_items
                ),
                "http_status_counts": status_counts,
            }
        )
        for item in run_agent.execution_items:
            if item.status != ExecutionStatus.FAILED:
                continue
            error_type = item.error_type or "unknown"
            failures_by_error[error_type] = failures_by_error.get(error_type, 0) + 1
    return {
        "run_id": str(run.id),
        "name": run.name,
        "status": run.status.value,
        "dataset_bundle_hash": run.dataset_bundle_hash,
        "agent_count": len(run.run_agents),
        "question_count_per_agent": len(items) // max(1, len(run.run_agents)),
        "execution_item_count": len(items),
        "failed_item_count": failed_count,
        "failure_rate": round(failed_count * 100 / len(items), 2) if items else 0,
        "failures_by_error_type": failures_by_error,
        "per_agent": per_agent,
        "agent_http_call_count": sum(len(item.attempts) for item in items),
        "retried_item_count": sum(item.attempt_count >= 2 for item in items),
        "judge_output_count": judge_outputs or 0,
        "citation_count": len(citations),
        "citation_unreachable_count": unreachable_count,
        "citation_unreachable_rate": (
            round(unreachable_count * 100 / len(citations), 2) if citations else 0
        ),
        "artifact_bytes": int(artifact_bytes or 0),
        "duration_seconds": duration_seconds,
        "snapshot_id": str(snapshot_id) if snapshot_id is not None else None,
    }
