import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from agent_eval.core.settings import Settings
from agent_eval.db.resource_leases import LeaseGrant, ResourceLeaseManager


class ResourceLimiter:
    def __init__(
        self,
        local_limit: int,
        *,
        resource_type: str,
        default_key: str | None = None,
        manager: ResourceLeaseManager | None = None,
        poll_seconds: float = 0.1,
    ) -> None:
        self._local_limit = local_limit
        self._resource_type = resource_type
        self._default_key = default_key
        self._manager = manager
        self._poll_seconds = poll_seconds
        self._local_slots: dict[str, asyncio.Semaphore] = {}

    @asynccontextmanager
    async def slot(self, resource_key: str | None = None) -> AsyncIterator[None]:
        key = resource_key or self._default_key
        if key is None:
            raise ValueError("a resource key is required")
        semaphore = self._local_slots.setdefault(key, asyncio.Semaphore(self._local_limit))
        async with semaphore:
            if self._manager is None:
                yield
                return
            grant = await self._wait_for_grant(key)
            owner_task = asyncio.current_task()
            if owner_task is None:
                await self._manager.release(grant)
                raise RuntimeError("resource limiter requires an asyncio task")
            heartbeat = asyncio.create_task(
                self._heartbeat(grant, owner_task),
                name=f"resource-lease-heartbeat-{self._resource_type}",
            )
            try:
                yield
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                await self._release(grant)

    async def _wait_for_grant(self, resource_key: str) -> LeaseGrant:
        assert self._manager is not None
        delay = self._poll_seconds
        maximum_delay = max(self._poll_seconds, min(2.0, self._poll_seconds * 8))
        while True:
            acquire_task = asyncio.create_task(
                self._manager.acquire(self._resource_type, resource_key, self._local_limit)
            )
            try:
                grant = await asyncio.shield(acquire_task)
            except asyncio.CancelledError:
                grant = await acquire_task
                if grant is not None:
                    await self._release(grant)
                raise
            if grant is not None:
                return grant
            await asyncio.sleep(random.uniform(delay * 0.8, delay * 1.2))
            delay = min(maximum_delay, delay * 1.5)

    async def _heartbeat(self, grant: LeaseGrant, owner_task: asyncio.Task[object]) -> None:
        assert self._manager is not None
        interval = max(0.05, self._manager.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._manager.renew(grant)
            except asyncio.CancelledError:
                raise
            except Exception:
                owner_task.cancel()
                return
            if not renewed:
                owner_task.cancel()
                return

    async def _release(self, grant: LeaseGrant) -> None:
        assert self._manager is not None
        release_task = asyncio.create_task(self._manager.release(grant))
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError:
            await release_task
            raise


RequestLimit = asyncio.Semaphore | ResourceLimiter


@dataclass(frozen=True)
class WorkerLimits:
    agent_requests: ResourceLimiter
    judge_requests: ResourceLimiter
    citation_fetches: ResourceLimiter
    citation_domains: ResourceLimiter
    publishing: asyncio.Lock

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        engine: AsyncEngine | None = None,
        owner_id: str | None = None,
    ) -> "WorkerLimits":
        manager: ResourceLeaseManager | None = None
        if engine is not None and engine.dialect.name == "postgresql":
            if owner_id is None:
                raise ValueError("PostgreSQL worker limits require an owner ID")
            manager = ResourceLeaseManager(engine, owner_id, settings.resource_lease_seconds)
        return cls(
            agent_requests=ResourceLimiter(
                settings.worker_agent_concurrency,
                resource_type="request",
                default_key="agent",
                manager=manager,
                poll_seconds=settings.resource_lease_poll_seconds,
            ),
            judge_requests=ResourceLimiter(
                settings.deepseek_concurrency,
                resource_type="request",
                default_key="judge",
                manager=manager,
                poll_seconds=settings.resource_lease_poll_seconds,
            ),
            citation_fetches=ResourceLimiter(
                settings.citation_fetch_concurrency,
                resource_type="request",
                default_key="citation-fetch",
                manager=manager,
                poll_seconds=settings.resource_lease_poll_seconds,
            ),
            citation_domains=ResourceLimiter(
                settings.citation_domain_concurrency,
                resource_type="citation-domain",
                manager=manager,
                poll_seconds=settings.resource_lease_poll_seconds,
            ),
            publishing=asyncio.Lock(),
        )


@asynccontextmanager
async def concurrency_slot(
    limiter: RequestLimit | None, resource_key: str | None = None
) -> AsyncIterator[None]:
    if limiter is None:
        yield
        return
    if isinstance(limiter, ResourceLimiter):
        async with limiter.slot(resource_key):
            yield
        return
    async with limiter:
        yield
