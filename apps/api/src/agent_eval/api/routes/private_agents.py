import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID, uuid4

import httpx
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agent_eval.agents.client import ConnectionConfig, ConnectionTestResult, test_connection
from agent_eval.agents.private_presets import (
    PRIVATE_AGENT_PRESETS,
    normalize_private_model,
    private_preset_request_options,
    private_request_template,
)
from agent_eval.api.dependencies import DatabaseSession, WorkspaceCsrf, WorkspaceSession
from agent_eval.api.mutation_guard import sqlite_mutation_guard
from agent_eval.api.routes.workspace import workspace_user_id
from agent_eval.api.schemas.private_agents import (
    PrivateAgentCreate,
    PrivateAgentDelete,
    PrivateAgentList,
    PrivateAgentPatch,
    PrivateAgentResponse,
    PrivateAgentTestResponse,
)
from agent_eval.auth.encryption import TokenCipher
from agent_eval.db.base import ensure_utc, utc_now
from agent_eval.db.enums import (
    AgentStatus,
    AgentVersionLifecycle,
    AuthType,
    PreflightStatus,
    PrivateAgentHealth,
    PrivateAgentPreset,
    WorkspaceAttemptStatus,
)
from agent_eval.db.models import Agent, AgentVersion, AuthUser, WorkspaceAttempt
from agent_eval.worker.limits import ResourceLimiter, concurrency_slot

router = APIRouter(prefix="/api/v1/workspace/private-agents", tags=["workspace-private-agents"])
PRIVATE_AGENT_LIMIT = 10
PRIVATE_AGENT_TEST_PROMPT = "Reply with exactly OK."


def private_agent_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
        headers={"Cache-Control": "private, no-store"},
    )


def require_cipher(request: Request) -> TokenCipher:
    cipher: TokenCipher | None = request.app.state.token_cipher
    if cipher is None:
        raise private_agent_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "encryption_not_configured",
            "Private Agent encryption is unavailable",
        )
    return cipher


def test_transport(request: Request) -> httpx.AsyncBaseTransport | None:
    return getattr(request.app.state, "agent_http_transport", None)


def request_limit(request: Request) -> ResourceLimiter:
    return cast(ResourceLimiter, request.app.state.agent_request_limit)


@asynccontextmanager
async def private_test_slot(request: Request, owner_user_id: UUID) -> AsyncIterator[None]:
    locks: dict[UUID, asyncio.Lock] = getattr(request.app.state, "private_agent_test_locks", {})
    request.app.state.private_agent_test_locks = locks
    lock = locks.setdefault(owner_user_id, asyncio.Lock())
    if lock.locked():
        raise private_agent_error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "private_agent_test_active",
            "A private Agent connection test is already active",
        )
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()
        if locks.get(owner_user_id) is lock and not lock.locked():
            locks.pop(owner_user_id, None)


def private_agent_response(
    agent: Agent, version: AgentVersion, versions: list[AgentVersion]
) -> PrivateAgentResponse:
    if version.private_preset is None or version.private_model is None:
        raise RuntimeError("private Agent has an invalid version")
    active = max(
        (
            item
            for item in versions
            if item.enabled
            and item.lifecycle_status == AgentVersionLifecycle.ACTIVE
            and item.private_health_status == PrivateAgentHealth.HEALTHY
        ),
        key=lambda item: item.version,
        default=None,
    )
    return PrivateAgentResponse(
        agent_id=agent.id,
        agent_version_id=version.id,
        active_agent_version_id=active.id if active is not None else None,
        active_provider=(
            PRIVATE_AGENT_PRESETS[active.private_preset].provider
            if active is not None and active.private_preset is not None
            else None
        ),
        active_model=active.private_model if active is not None else None,
        name=agent.name,
        description=agent.description,
        preset=version.private_preset,
        provider=PRIVATE_AGENT_PRESETS[version.private_preset].provider,
        model=version.private_model,
        version=version.version,
        health_status=version.private_health_status,
        health_checked_at=version.private_health_checked_at,
        health_error_code=version.private_health_error_code,
        health_latency_ms=version.private_health_latency_ms,
        workspace_available=active is not None,
        created_at=agent.created_at,
        updated_at=max(ensure_utc(agent.updated_at), ensure_utc(version.updated_at)),
    )


def configured_version(
    *,
    agent_id: UUID,
    version_id: UUID,
    version_number: int,
    preset: PrivateAgentPreset,
    model: str,
    encrypted_token: str,
) -> AgentVersion:
    preset_config = PRIVATE_AGENT_PRESETS[preset]
    model = normalize_private_model(preset, model)
    return AgentVersion(
        id=version_id,
        agent_id=agent_id,
        version=version_number,
        endpoint_url=preset_config.endpoint_url,
        auth_type=AuthType.BEARER,
        encrypted_token=encrypted_token,
        request_template=private_request_template(model),
        answer_jsonpath="$.choices[0].message.content",
        citations_jsonpath=None,
        timeout_seconds=120,
        concurrency=1,
        lifecycle_status=AgentVersionLifecycle.DRAFT,
        preflight_status=PreflightStatus.NOT_RUN,
        enabled=False,
        private_preset=preset,
        private_model=model,
        private_health_status=PrivateAgentHealth.UNTESTED,
    )


async def owned_private_agent_versions(
    database: AsyncSession,
    agent_id: UUID,
    owner_user_id: UUID,
    *,
    lock: bool,
) -> tuple[Agent, list[AgentVersion]]:
    statement = select(Agent).where(
        Agent.id == agent_id,
        Agent.owner_user_id == owner_user_id,
    )
    if lock:
        statement = statement.with_for_update(of=Agent)
    agent = await database.scalar(statement)
    if agent is None:
        raise private_agent_error(404, "private_agent_not_found", "Private Agent not found")
    version_statement = (
        select(AgentVersion).where(AgentVersion.agent_id == agent.id).order_by(AgentVersion.version)
    )
    if lock:
        version_statement = version_statement.with_for_update(of=AgentVersion)
    versions = list(await database.scalars(version_statement))
    if not versions:
        raise RuntimeError("private Agent has no version")
    return agent, versions


def ensure_expected_version(latest: AgentVersion, expected_version: int) -> None:
    if latest.version != expected_version:
        raise private_agent_error(409, "version_conflict", "Private Agent version changed")


def safe_test_error(result: ConnectionTestResult) -> str | None:
    if result.success:
        return None
    if result.status_code in {401, 403}:
        return "authentication_failed"
    if result.status_code == 402:
        return "balance_exhausted"
    if result.status_code == 429:
        return "rate_limited"
    if result.status_code == 408 or result.error == "agent_timeout":
        return "timeout"
    if result.status_code is not None and result.status_code >= 500:
        return "provider_unavailable"
    if result.status_code is not None and result.status_code >= 400:
        return "request_rejected"
    if result.error == "agent_network_error":
        return "network_error"
    return "invalid_response"


@router.get("", response_model=PrivateAgentList)
async def list_private_agents(
    response: Response,
    database: DatabaseSession,
    auth_session: WorkspaceSession,
) -> PrivateAgentList:
    agents = list(
        await database.scalars(
            select(Agent)
            .options(selectinload(Agent.versions))
            .where(
                Agent.owner_user_id == workspace_user_id(auth_session),
                Agent.status != AgentStatus.DISABLED,
            )
            .order_by(Agent.created_at.desc(), Agent.id.desc())
        )
    )
    response.headers["Cache-Control"] = "private, no-store"
    return PrivateAgentList(
        items=[
            private_agent_response(
                agent,
                max(agent.versions, key=lambda item: item.version),
                list(agent.versions),
            )
            for agent in agents
        ]
    )


@router.post("", response_model=PrivateAgentResponse, status_code=status.HTTP_201_CREATED)
async def create_private_agent(
    payload: PrivateAgentCreate,
    request: Request,
    response: Response,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> PrivateAgentResponse:
    owner_user_id = workspace_user_id(auth_session)
    cipher = require_cipher(request)
    async with sqlite_mutation_guard(request, database, f"private-agent-create:{owner_user_id}"):
        await database.execute(
            select(AuthUser.id).where(AuthUser.id == owner_user_id).with_for_update()
        )
        count = await database.scalar(
            select(func.count(Agent.id)).where(
                Agent.owner_user_id == owner_user_id,
                Agent.status != AgentStatus.DISABLED,
            )
        )
        if int(count or 0) >= PRIVATE_AGENT_LIMIT:
            raise private_agent_error(
                409, "private_agent_limit_reached", "Private Agent limit reached"
            )
        agent_id = uuid4()
        version_id = uuid4()
        agent = Agent(
            id=agent_id,
            owner_user_id=owner_user_id,
            name=payload.name,
            provider=PRIVATE_AGENT_PRESETS[payload.preset].provider,
            description=payload.description,
            status=AgentStatus.ENABLED,
        )
        version = configured_version(
            agent_id=agent_id,
            version_id=version_id,
            version_number=1,
            preset=payload.preset,
            model=payload.model,
            encrypted_token=cipher.encrypt_private(
                payload.api_key.get_secret_value(), owner_user_id, version_id
            ),
        )
        database.add_all([agent, version])
        try:
            await database.commit()
        except IntegrityError as error:
            await database.rollback()
            raise private_agent_error(
                409, "private_agent_name_exists", "Private Agent name already exists"
            ) from error
    response.headers["Cache-Control"] = "private, no-store"
    return private_agent_response(agent, version, [version])


@router.patch("/{agent_id}", response_model=PrivateAgentResponse)
async def patch_private_agent(
    agent_id: UUID,
    payload: PrivateAgentPatch,
    request: Request,
    response: Response,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> PrivateAgentResponse:
    owner_user_id = workspace_user_id(auth_session)
    async with sqlite_mutation_guard(request, database, f"private-agent:{agent_id}"):
        agent, versions = await owned_private_agent_versions(
            database, agent_id, owner_user_id, lock=True
        )
        if agent.status == AgentStatus.DISABLED:
            raise private_agent_error(409, "private_agent_revoked", "Private Agent is revoked")
        latest = versions[-1]
        ensure_expected_version(latest, payload.expected_version)
        if payload.name is not None:
            agent.name = payload.name
        if "description" in payload.model_fields_set:
            agent.description = payload.description

        preset = payload.preset or latest.private_preset
        requested_model = payload.model or latest.private_model
        if preset is None or requested_model is None:
            raise RuntimeError("private Agent has an invalid latest version")
        model = normalize_private_model(preset, requested_model)
        config_changed = (
            (payload.preset is not None and payload.preset != latest.private_preset)
            or model != latest.private_model
            or "api_key" in payload.model_fields_set
        )
        if config_changed:
            cipher = require_cipher(request)
            if payload.api_key is not None:
                token = payload.api_key.get_secret_value()
            else:
                if latest.encrypted_token is None:
                    raise private_agent_error(
                        503,
                        "private_agent_key_unavailable",
                        "Private Agent key is unavailable",
                    )
                try:
                    token = cipher.decrypt_private(latest.encrypted_token, owner_user_id, latest.id)
                except (InvalidTag, UnicodeError, ValueError) as error:
                    raise private_agent_error(
                        503,
                        "private_agent_key_unavailable",
                        "Private Agent key is unavailable",
                    ) from error
            version_id = uuid4()
            latest = configured_version(
                agent_id=agent.id,
                version_id=version_id,
                version_number=versions[-1].version + 1,
                preset=preset,
                model=model,
                encrypted_token=cipher.encrypt_private(token, owner_user_id, version_id),
            )
            database.add(latest)
        try:
            await database.commit()
        except IntegrityError as error:
            await database.rollback()
            raise private_agent_error(
                409, "private_agent_name_exists", "Private Agent name already exists"
            ) from error
    response.headers["Cache-Control"] = "private, no-store"
    response_versions = versions if latest in versions else [*versions, latest]
    return private_agent_response(agent, latest, response_versions)


@router.post("/{agent_id}/test", response_model=PrivateAgentTestResponse)
async def test_private_agent(
    agent_id: UUID,
    request: Request,
    response: Response,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> PrivateAgentTestResponse:
    owner_user_id = workspace_user_id(auth_session)
    cipher = require_cipher(request)
    async with private_test_slot(request, owner_user_id):
        async with sqlite_mutation_guard(request, database, f"private-agent:{agent_id}"):
            agent, versions = await owned_private_agent_versions(
                database, agent_id, owner_user_id, lock=True
            )
            if agent.status == AgentStatus.DISABLED:
                raise private_agent_error(409, "private_agent_revoked", "Private Agent is revoked")
            latest = versions[-1]
            if latest.encrypted_token is None:
                raise private_agent_error(
                    503,
                    "private_agent_key_unavailable",
                    "Private Agent key is unavailable",
                )
            try:
                token = cipher.decrypt_private(latest.encrypted_token, owner_user_id, latest.id)
            except (InvalidTag, UnicodeError, ValueError) as error:
                raise private_agent_error(
                    503,
                    "private_agent_key_unavailable",
                    "Private Agent key is unavailable",
                ) from error
            extra_headers, api_key_header = private_preset_request_options(latest.private_preset)
            config = ConnectionConfig(
                endpoint_url=latest.endpoint_url,
                auth_type=AuthType.BEARER,
                token=token,
                request_template=latest.request_template,
                answer_jsonpath=latest.answer_jsonpath,
                citations_jsonpath=None,
                timeout_seconds=120,
                extra_headers=extra_headers,
                api_key_header=api_key_header,
            )
            tested_version_id = latest.id
            await database.commit()

        async with concurrency_slot(request_limit(request)):
            result = await test_connection(
                config,
                PRIVATE_AGENT_TEST_PROMPT,
                transport=test_transport(request),
            )
        checked_at = utc_now()
        error_code = safe_test_error(result)
        async with sqlite_mutation_guard(request, database, f"private-agent:{agent_id}"):
            agent, versions = await owned_private_agent_versions(
                database, agent_id, owner_user_id, lock=True
            )
            latest = versions[-1]
            if (
                agent.status == AgentStatus.DISABLED
                or latest.id != tested_version_id
                or latest.encrypted_token is None
                or latest.private_health_status == PrivateAgentHealth.REVOKED
            ):
                await database.rollback()
                raise private_agent_error(
                    409,
                    "private_agent_changed_during_test",
                    "Private Agent changed or was revoked during test",
                )
            latest.private_health_checked_at = checked_at
            latest.private_health_latency_ms = result.latency_ms
            latest.private_health_error_code = error_code
            if result.success:
                for version in versions:
                    if (
                        version is not latest
                        and version.lifecycle_status == AgentVersionLifecycle.ACTIVE
                    ):
                        version.lifecycle_status = AgentVersionLifecycle.RETIRED
                        version.enabled = False
                latest.lifecycle_status = AgentVersionLifecycle.ACTIVE
                latest.enabled = True
                latest.private_health_status = PrivateAgentHealth.HEALTHY
                assert latest.private_preset is not None
                agent.provider = PRIVATE_AGENT_PRESETS[latest.private_preset].provider
            else:
                latest.lifecycle_status = AgentVersionLifecycle.DRAFT
                latest.enabled = False
                latest.private_health_status = PrivateAgentHealth.UNHEALTHY
            await database.commit()
    response.headers["Cache-Control"] = "private, no-store"
    config_response = private_agent_response(agent, latest, versions)
    return PrivateAgentTestResponse(
        **config_response.model_dump(),
        success=result.success,
        status_code=result.status_code,
        latency_ms=result.latency_ms,
        error_code=error_code,
    )


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_private_agent(
    agent_id: UUID,
    payload: PrivateAgentDelete,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> Response:
    owner_user_id = workspace_user_id(auth_session)
    async with sqlite_mutation_guard(request, database, f"private-agent:{agent_id}"):
        agent, versions = await owned_private_agent_versions(
            database, agent_id, owner_user_id, lock=True
        )
        latest = versions[-1]
        ensure_expected_version(latest, payload.expected_version)
        version_ids = [version.id for version in versions]
        now = utc_now()
        await database.execute(
            update(WorkspaceAttempt)
            .where(
                WorkspaceAttempt.agent_version_id.in_(version_ids),
                WorkspaceAttempt.status == WorkspaceAttemptStatus.QUEUED,
            )
            .values(
                status=WorkspaceAttemptStatus.CANCELED,
                cancel_requested_at=now,
                finished_at=now,
                error_type="workspace_canceled",
                error_category="canceled",
                updated_at=now,
            )
        )
        await database.execute(
            update(WorkspaceAttempt)
            .where(
                WorkspaceAttempt.agent_version_id.in_(version_ids),
                WorkspaceAttempt.status == WorkspaceAttemptStatus.RUNNING,
            )
            .values(cancel_requested_at=now, updated_at=now)
        )
        agent.status = AgentStatus.DISABLED
        for version in versions:
            version.lifecycle_status = AgentVersionLifecycle.RETIRED
            version.enabled = False
            version.encrypted_token = None
            version.private_health_status = PrivateAgentHealth.REVOKED
        await database.commit()
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"Cache-Control": "private, no-store"},
    )
