import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select

from agent_eval.auth.identity import LEGACY_OPERATOR_USER_ID
from agent_eval.core.settings import Settings
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import (
    AuthType,
    RunStatus,
    WorkspaceAttemptStatus,
    WorkspaceMessageRole,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    EvaluationRun,
    JudgeConfig,
    RunAgent,
    ScoreConfig,
    WorkspaceAttempt,
    WorkspaceConversation,
    WorkspaceMessage,
)
from agent_eval.db.resource_leases import ResourceLeaseManager
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.runs.queue import acquire_worker_lease, claim_next_run
from agent_eval.worker.limits import ResourceLimiter, concurrency_slot
from agent_eval.workspace.queue import claim_next_workspace_attempt

POSTGRES_URL = os.getenv("AGENT_EVAL_TEST_POSTGRES_URL")


def migration_config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.mark.postgresql
@pytest.mark.skipif(not POSTGRES_URL, reason="AGENT_EVAL_TEST_POSTGRES_URL is not set")
async def test_postgresql_fresh_migration_and_concurrent_claims() -> None:
    assert POSTGRES_URL is not None
    config = migration_config(POSTGRES_URL)
    await asyncio.to_thread(command.downgrade, config, "base")
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_engine(Settings(database_url=POSTGRES_URL))
    session_factory = create_session_factory(engine)

    try:
        async with session_scope(session_factory) as session:
            judge = JudgeConfig(provider="judge", model="model", prompt_version="v1")
            score = ScoreConfig(name="PostgreSQL concurrency")
            provider_a = Agent(name="Provider A Alpha", provider="Provider A")
            provider_a_second = Agent(name="Provider A Beta", provider="Provider A")
            provider_b = Agent(name="Provider B Gamma", provider="Provider B")
            session.add_all([judge, score, provider_a, provider_a_second, provider_b])
            await session.flush()
            versions = [
                AgentVersion(
                    agent_id=agent.id,
                    version=1,
                    endpoint_url="https://agent.example/run",
                    auth_type=AuthType.NONE,
                    request_template='{"query":"{{prompt}}"}',
                    answer_jsonpath="$.answer",
                )
                for agent in (provider_a, provider_a_second, provider_b)
            ]
            session.add_all(versions)
            await session.flush()

            run_versions = [versions[0], versions[0], versions[1], versions[2]]
            for index, version in enumerate(run_versions):
                run = EvaluationRun(
                    name=f"Concurrent run {index}",
                    status=RunStatus.QUEUED,
                    dataset_bundle_hash=str(index) * 64,
                    judge_config_id=judge.id,
                    score_config_id=score.id,
                )
                session.add(run)
                await session.flush()
                session.add(
                    RunAgent(run_id=run.id, agent_version_id=version.id, total=0)
                )
            for index in range(2):
                conversation = WorkspaceConversation(
                    owner_user_id=LEGACY_OPERATOR_USER_ID,
                    pinned_agent_version_id=versions[index].id,
                    title=f"PostgreSQL workspace {index}",
                    last_turn_no=1,
                    revision=1,
                )
                session.add(conversation)
                await session.flush()
                message = WorkspaceMessage(
                    conversation_id=conversation.id,
                    turn_no=1,
                    role=WorkspaceMessageRole.USER,
                    variant_no=1,
                    active=True,
                    content=f"Workspace question {index}",
                )
                session.add(message)
                await session.flush()
                session.add(
                    WorkspaceAttempt(
                        conversation_id=conversation.id,
                        user_message_id=message.id,
                        agent_version_id=versions[index].id,
                        generation_no=1,
                        idempotency_key=str(uuid4()),
                        request_fingerprint=str(index) * 64,
                        status=WorkspaceAttemptStatus.QUEUED,
                        prompt_hash=str(index + 1) * 64,
                        prompt_characters=100,
                        prompt_format="workspace-transcript-v1",
                    )
                )

        assert await asyncio.gather(
            acquire_worker_lease(engine, "postgres-worker-a"),
            acquire_worker_lease(engine, "postgres-worker-b"),
        ) == [True, True]
        claims = await asyncio.gather(
            claim_next_run(
                engine, "postgres-worker-a", max_active_runs=3, max_provider_runs=1
            ),
            claim_next_run(
                engine, "postgres-worker-b", max_active_runs=3, max_provider_runs=1
            ),
        )
        assert None not in claims
        assert len(set(claims)) == 2
        assert (
            await claim_next_run(
                engine, "postgres-worker-a", max_active_runs=3, max_provider_runs=1
            )
            is None
        )

        async with session_factory() as session:
            active = list(
                await session.scalars(
                    select(EvaluationRun).where(EvaluationRun.active_slot.is_not(None))
                )
            )
        assert len(active) == 2
        assert {run.active_slot for run in active} == {1, 2}
        assert {run.claim_owner_id for run in active} == {
            "postgres-worker-a",
            "postgres-worker-b",
        }
        assert len({run.id for run in active}) == 2

        workspace_claims = await asyncio.gather(
            claim_next_workspace_attempt(
                engine, "postgres-worker-a", lease_seconds=30
            ),
            claim_next_workspace_attempt(
                engine, "postgres-worker-b", lease_seconds=30
            ),
        )
        assert None not in workspace_claims
        assert len({claim.attempt_id for claim in workspace_claims if claim is not None}) == 2

        second_engine = create_engine(Settings(database_url=POSTGRES_URL))
        active_requests = 0
        maximum_requests = 0
        counter_lock = asyncio.Lock()
        managers = [
            ResourceLeaseManager(engine, "postgres-limit-a", lease_seconds=2),
            ResourceLeaseManager(second_engine, "postgres-limit-b", lease_seconds=2),
        ]
        limiters = [
            ResourceLimiter(
                3,
                resource_type="request",
                default_key="judge",
                manager=manager,
                poll_seconds=0.01,
            )
            for manager in managers
        ]

        async def request(index: int) -> None:
            nonlocal active_requests, maximum_requests
            async with concurrency_slot(limiters[index % 2]):
                async with counter_lock:
                    active_requests += 1
                    maximum_requests = max(maximum_requests, active_requests)
                await asyncio.sleep(0.03)
                async with counter_lock:
                    active_requests -= 1

        try:
            await asyncio.gather(*(request(index) for index in range(20)))
            assert maximum_requests == 3
        finally:
            await second_engine.dispose()
    finally:
        async with engine.begin() as connection:
            await connection.execute(delete(EvaluationRun))
        await engine.dispose()
        await asyncio.to_thread(command.downgrade, config, "base")
