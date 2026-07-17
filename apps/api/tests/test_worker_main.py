import asyncio
from collections import defaultdict
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agent_eval.core.artifact_store import ArtifactStore
from agent_eval.core.settings import Settings
from agent_eval.worker import main as worker_main
from agent_eval.worker.limits import WorkerLimits, concurrency_slot


async def test_run_pipelines_execute_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    run_ids = [uuid4(), uuid4()]
    started: set[UUID] = set()
    phases: dict[UUID, list[str]] = defaultdict(list)
    both_started = asyncio.Event()
    release_inference = asyncio.Event()
    active_publishes = 0
    max_active_publishes = 0
    settings = Settings(environment="test")
    limits = WorkerLimits.from_settings(settings)
    artifact_store = cast(ArtifactStore, object())

    async def inference(*args: Any, **kwargs: Any) -> bool:
        run_id = cast(UUID, args[2])
        assert kwargs["request_limit"] is limits.agent_requests
        phases[run_id].append("inference")
        started.add(run_id)
        if len(started) == 2:
            both_started.set()
        await release_inference.wait()
        return True

    def phase(name: str) -> Any:
        async def execute(*args: Any, **kwargs: Any) -> bool:
            run_id = cast(UUID, args[2])
            expected_limit = (
                limits.citation_fetches if name == "citation_fetch" else limits.judge_requests
            )
            assert kwargs["request_limit"] is expected_limit
            assert kwargs["artifact_store"] is artifact_store
            phases[run_id].append(name)
            return True

        return execute

    async def aggregation(*args: Any, **kwargs: Any) -> bool:
        nonlocal active_publishes, max_active_publishes
        run_id = cast(UUID, args[1])
        phases[run_id].append("aggregation")
        active_publishes += 1
        max_active_publishes = max(max_active_publishes, active_publishes)
        try:
            await asyncio.sleep(0.01)
            return True
        finally:
            active_publishes -= 1

    monkeypatch.setattr(worker_main, "execute_run_inference", inference)
    monkeypatch.setattr(worker_main, "execute_answer_scoring", phase("answer_scoring"))
    monkeypatch.setattr(worker_main, "execute_citation_fetch", phase("citation_fetch"))
    monkeypatch.setattr(worker_main, "execute_citation_scoring", phase("citation_scoring"))
    monkeypatch.setattr(worker_main, "execute_aggregation_and_publish", aggregation)

    engine = cast(AsyncEngine, object())
    session_factory = cast(async_sessionmaker[AsyncSession], object())
    tasks = [
        asyncio.create_task(
            worker_main.process_run(
                engine,
                session_factory,
                settings,
                run_id,
                artifact_store=artifact_store,
                token_cipher=None,
                owner_id="worker",
                limits=limits,
                initial_phase="inference",
            )
        )
        for run_id in run_ids
    ]
    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert all(not task.done() for task in tasks)

    release_inference.set()
    assert await asyncio.gather(*tasks) == [True, True]
    assert max_active_publishes == 1
    assert phases == {
        run_id: [
            "inference",
            "answer_scoring",
            "citation_fetch",
            "citation_scoring",
            "aggregation",
        ]
        for run_id in run_ids
    }


async def test_worker_limits_are_shared_across_tasks() -> None:
    limits = WorkerLimits.from_settings(
        Settings(
            environment="test",
            worker_agent_concurrency=1,
            deepseek_concurrency=1,
            citation_fetch_concurrency=1,
            citation_domain_concurrency=1,
        )
    )
    active = 0
    max_active = 0

    async def request() -> None:
        nonlocal active, max_active
        async with concurrency_slot(limits.agent_requests):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(request() for _ in range(3)))
    assert max_active == 1
