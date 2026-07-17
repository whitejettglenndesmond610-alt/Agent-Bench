import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_eval.db.base import ensure_utc
from agent_eval.db.models import ResourceLease


@dataclass(frozen=True)
class LeaseGrant:
    resource_type: str
    resource_key: str
    slot_number: int
    holder_token: str


class ResourceLeaseManager:
    def __init__(self, engine: AsyncEngine, owner_id: str, lease_seconds: float) -> None:
        if not owner_id or len(owner_id) > 120:
            raise ValueError("resource lease owner ID must contain 1 to 120 characters")
        if lease_seconds <= 0:
            raise ValueError("resource lease duration must be positive")
        self._engine = engine
        self._owner_id = owner_id
        self.lease_seconds = lease_seconds

    async def acquire(
        self, resource_type: str, resource_key: str, limit: int
    ) -> LeaseGrant | None:
        self._validate_resource(resource_type, resource_key, limit)
        token = uuid4().hex
        try:
            async with self._engine.begin() as connection:
                if connection.dialect.name == "postgresql":
                    await connection.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                        {
                            "lock_key": json.dumps(
                                [resource_type, resource_key], separators=(",", ":")
                            )
                        },
                    )
                await connection.execute(
                    delete(ResourceLease).where(
                        ResourceLease.resource_type == resource_type,
                        ResourceLease.resource_key == resource_key,
                        ResourceLease.lease_expires_at <= func.current_timestamp(),
                    )
                )
                occupied = set(
                    (
                        await connection.execute(
                            select(ResourceLease.slot_number).where(
                                ResourceLease.resource_type == resource_type,
                                ResourceLease.resource_key == resource_key,
                                ResourceLease.slot_number <= limit,
                            )
                        )
                    ).scalars()
                )
                slot_number = next(
                    (candidate for candidate in range(1, limit + 1) if candidate not in occupied),
                    None,
                )
                if slot_number is None:
                    return None
                database_now = ensure_utc(
                    (await connection.execute(select(func.current_timestamp()))).scalar_one()
                )
                await connection.execute(
                    insert(ResourceLease).values(
                        resource_type=resource_type,
                        resource_key=resource_key,
                        slot_number=slot_number,
                        holder_token=token,
                        owner_id=self._owner_id,
                        acquired_at=database_now,
                        lease_expires_at=database_now + timedelta(seconds=self.lease_seconds),
                    )
                )
            return LeaseGrant(resource_type, resource_key, slot_number, token)
        except IntegrityError:
            return None

    async def renew(self, grant: LeaseGrant) -> bool:
        async with self._engine.begin() as connection:
            database_now = ensure_utc(
                (await connection.execute(select(func.current_timestamp()))).scalar_one()
            )
            result = await connection.execute(
                update(ResourceLease)
                .where(
                    ResourceLease.holder_token == grant.holder_token,
                    ResourceLease.resource_type == grant.resource_type,
                    ResourceLease.resource_key == grant.resource_key,
                    ResourceLease.slot_number == grant.slot_number,
                    ResourceLease.lease_expires_at > func.current_timestamp(),
                )
                .values(lease_expires_at=database_now + timedelta(seconds=self.lease_seconds))
            )
            return result.rowcount == 1

    async def release(self, grant: LeaseGrant) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(ResourceLease).where(
                    ResourceLease.holder_token == grant.holder_token,
                    ResourceLease.resource_type == grant.resource_type,
                    ResourceLease.resource_key == grant.resource_key,
                    ResourceLease.slot_number == grant.slot_number,
                )
            )

    @staticmethod
    def _validate_resource(resource_type: str, resource_key: str, limit: int) -> None:
        if not resource_type or len(resource_type) > 40:
            raise ValueError("resource type must contain 1 to 40 characters")
        if not resource_key or len(resource_key) > 255:
            raise ValueError("resource key must contain 1 to 255 characters")
        if limit < 1:
            raise ValueError("resource limit must be positive")
