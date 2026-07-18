from typing import cast
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from agent_eval.agents.client import ConnectionConfig, ConnectionTestResult, test_connection
from agent_eval.agents.preflight import run_agent_preflight
from agent_eval.api.dependencies import DatabaseSession, OperatorCsrfSession, OperatorSession
from agent_eval.api.schemas.agents import (
    AgentConnectionTestRequest,
    AgentCreate,
    AgentDetailResponse,
    AgentResponse,
    AgentVersionCreate,
    AgentVersionPreflightResponse,
    AgentVersionResponse,
    ConnectionTestResponse,
    PersistedAgentTestRequest,
    PreflightCheck,
)
from agent_eval.auth.encryption import TokenCipher
from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    AgentVersionLifecycle,
    AuthType,
    DatasetStatus,
    PreflightStatus,
)
from agent_eval.db.models import Agent, AgentVersion, DatasetVersion, Question
from agent_eval.worker.limits import ResourceLimiter, concurrency_slot

router = APIRouter(prefix="/api/v1", tags=["agents"])


def version_response(version: AgentVersion) -> AgentVersionResponse:
    return AgentVersionResponse(
        id=version.id,
        agent_id=version.agent_id,
        version=version.version,
        endpoint_url=version.endpoint_url,
        auth_type=version.auth_type,
        token_configured=version.encrypted_token is not None,
        request_template=version.request_template,
        answer_jsonpath=version.answer_jsonpath,
        citations_jsonpath=version.citations_jsonpath,
        timeout_seconds=version.timeout_seconds,
        concurrency=version.concurrency,
        max_prompt_characters=version.max_prompt_characters,
        lifecycle_status=version.lifecycle_status,
        preflight_status=version.preflight_status,
        preflight_report=[
            PreflightCheck.model_validate(item)
            for item in (version.preflight_report_json or [])
        ],
        preflight_at=version.preflight_at,
        enabled=version.enabled,
        created_at=version.created_at,
    )


def detail_response(agent: Agent) -> AgentDetailResponse:
    return AgentDetailResponse(
        id=agent.id,
        name=agent.name,
        provider=agent.provider,
        description=agent.description,
        logo_url=agent.logo_url,
        status=agent.status,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        versions=[
            version_response(version)
            for version in sorted(agent.versions, key=lambda item: item.version, reverse=True)
        ],
    )


async def get_agent_or_404(database: DatabaseSession, agent_id: UUID) -> Agent:
    agent = await database.scalar(
        select(Agent).options(selectinload(Agent.versions)).where(Agent.id == agent_id)
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "agent_not_found", "message": "Agent 不存在"},
        )
    return agent


async def get_version_or_404(database: DatabaseSession, version_id: UUID) -> AgentVersion:
    version = await database.get(AgentVersion, version_id)
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "agent_version_not_found", "message": "Agent 版本不存在"},
        )
    return version


def decrypt_version_token(
    version: AgentVersion,
    request: Request,
) -> str | None:
    if version.auth_type != AuthType.BEARER:
        return None
    if version.encrypted_token is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "agent_token_missing", "message": "Agent Token 数据缺失"},
        )
    try:
        return require_cipher(request).decrypt(version.encrypted_token)
    except (InvalidTag, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "agent_token_unavailable", "message": "Agent Token 无法解密"},
        ) from error


def require_cipher(request: Request) -> TokenCipher:
    cipher: TokenCipher | None = request.app.state.token_cipher
    if cipher is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "encryption_not_configured", "message": "Agent Token 加密密钥未配置"},
        )
    return cipher


def connection_config(
    payload: AgentVersionCreate | AgentConnectionTestRequest,
    token: str | None,
) -> ConnectionConfig:
    return ConnectionConfig(
        endpoint_url=str(payload.endpoint_url),
        auth_type=payload.auth_type,
        token=token,
        request_template=payload.request_template,
        answer_jsonpath=payload.answer_jsonpath,
        citations_jsonpath=payload.citations_jsonpath,
        timeout_seconds=payload.timeout_seconds,
    )


def test_response(result: ConnectionTestResult) -> ConnectionTestResponse:
    return ConnectionTestResponse(
        success=result.success,
        status_code=result.status_code,
        latency_ms=result.latency_ms,
        request_body=result.request_body,
        raw_response=result.raw_response,
        answer=result.answer,
        citations=result.citations,
        error=result.error,
    )


def test_transport(request: Request) -> httpx.AsyncBaseTransport | None:
    return getattr(request.app.state, "agent_http_transport", None)


def agent_request_limit(request: Request) -> ResourceLimiter:
    return cast(ResourceLimiter, request.app.state.agent_request_limit)


@router.get("/agents", response_model=list[AgentResponse])
async def list_agents(
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> list[Agent]:
    result = await database.scalars(select(Agent).order_by(Agent.created_at.desc()))
    return list(result)


@router.post("/agents", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> Agent:
    agent = Agent(
        name=payload.name,
        provider=payload.provider,
        description=payload.description,
        logo_url=str(payload.logo_url) if payload.logo_url is not None else None,
    )
    database.add(agent)
    try:
        await database.commit()
    except IntegrityError as error:
        await database.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "agent_name_exists", "message": "Agent 名称已存在"},
        ) from error
    await database.refresh(agent)
    return agent


@router.post("/agents/test-connection", response_model=ConnectionTestResponse)
async def test_unsaved_connection(
    payload: AgentConnectionTestRequest,
    request: Request,
    _authorization: OperatorCsrfSession,
) -> ConnectionTestResponse:
    token = payload.token.get_secret_value() if payload.token is not None else None
    async with concurrency_slot(agent_request_limit(request)):
        result = await test_connection(
            connection_config(payload, token),
            payload.prompt,
            transport=test_transport(request),
        )
    return test_response(result)


@router.get("/agents/{agent_id}", response_model=AgentDetailResponse)
async def get_agent(
    agent_id: UUID,
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> AgentDetailResponse:
    return detail_response(await get_agent_or_404(database, agent_id))


@router.post(
    "/agents/{agent_id}/versions",
    response_model=AgentVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_version(
    agent_id: UUID,
    payload: AgentVersionCreate,
    request: Request,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> AgentVersionResponse:
    await get_agent_or_404(database, agent_id)
    encrypted_token: str | None = None
    if payload.auth_type == AuthType.BEARER:
        token = payload.token
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "agent_token_required", "message": "Bearer 认证需要 Token"},
            )
        encrypted_token = require_cipher(request).encrypt(token.get_secret_value())

    next_version = await database.scalar(
        select(func.coalesce(func.max(AgentVersion.version), 0) + 1).where(
            AgentVersion.agent_id == agent_id
        )
    )
    version = AgentVersion(
        agent_id=agent_id,
        version=int(next_version or 1),
        endpoint_url=str(payload.endpoint_url),
        auth_type=payload.auth_type,
        encrypted_token=encrypted_token,
        request_template=payload.request_template,
        answer_jsonpath=payload.answer_jsonpath,
        citations_jsonpath=payload.citations_jsonpath,
        timeout_seconds=payload.timeout_seconds,
        concurrency=payload.concurrency,
        max_prompt_characters=payload.max_prompt_characters,
        lifecycle_status=AgentVersionLifecycle.DRAFT,
        preflight_status=PreflightStatus.NOT_RUN,
    )
    database.add(version)
    await database.commit()
    await database.refresh(version)
    return version_response(version)


@router.post("/agent-versions/{version_id}/test", response_model=ConnectionTestResponse)
async def test_saved_connection(
    version_id: UUID,
    payload: PersistedAgentTestRequest,
    request: Request,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> ConnectionTestResponse:
    version = await get_version_or_404(database, version_id)
    token = decrypt_version_token(version, request)

    config = ConnectionConfig(
        endpoint_url=version.endpoint_url,
        auth_type=version.auth_type,
        token=token,
        request_template=version.request_template,
        answer_jsonpath=version.answer_jsonpath,
        citations_jsonpath=version.citations_jsonpath,
        timeout_seconds=version.timeout_seconds,
    )
    async with concurrency_slot(agent_request_limit(request)):
        result = await test_connection(
            config,
            payload.prompt,
            transport=test_transport(request),
        )
    return test_response(result)


@router.post(
    "/agent-versions/{version_id}/preflight",
    response_model=AgentVersionPreflightResponse,
)
async def preflight_agent_version(
    version_id: UUID,
    request: Request,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> AgentVersionPreflightResponse:
    version = await get_version_or_404(database, version_id)
    if version.lifecycle_status not in {
        AgentVersionLifecycle.DRAFT,
        AgentVersionLifecycle.VERIFIED,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "preflight_not_allowed", "message": "只能预检候选版本"},
        )
    if version.preflight_status == PreflightStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "preflight_active", "message": "候选版本正在预检"},
        )
    token = decrypt_version_token(version, request)
    version.preflight_status = PreflightStatus.RUNNING
    await database.commit()

    questions = list(
        await database.scalars(
            select(Question)
            .options(joinedload(Question.dataset_version).joinedload(DatasetVersion.benchmark))
            .join(DatasetVersion, DatasetVersion.id == Question.dataset_version_id)
            .where(
                DatasetVersion.status == DatasetStatus.FROZEN,
                DatasetVersion.run_eligible.is_(True),
            )
        )
    )
    config = ConnectionConfig(
        endpoint_url=version.endpoint_url,
        auth_type=version.auth_type,
        token=token,
        request_template=version.request_template,
        answer_jsonpath=version.answer_jsonpath,
        citations_jsonpath=version.citations_jsonpath,
        timeout_seconds=version.timeout_seconds,
    )
    result = await run_agent_preflight(
        config,
        questions,
        max_prompt_characters=version.max_prompt_characters,
        transport=test_transport(request),
        request_limit=agent_request_limit(request),
    )
    checked_at = utc_now()
    version.preflight_status = (
        PreflightStatus.PASSED if result.passed else PreflightStatus.FAILED
    )
    version.lifecycle_status = (
        AgentVersionLifecycle.VERIFIED if result.passed else AgentVersionLifecycle.DRAFT
    )
    version.preflight_report_json = [check.as_dict() for check in result.checks]
    version.preflight_at = checked_at
    await database.commit()
    return AgentVersionPreflightResponse(
        version_id=version.id,
        lifecycle_status=version.lifecycle_status,
        preflight_status=version.preflight_status,
        checks=[PreflightCheck.model_validate(item) for item in version.preflight_report_json],
        preflight_at=checked_at,
    )


@router.post(
    "/agent-versions/{version_id}/promote",
    response_model=AgentVersionResponse,
)
async def promote_agent_version(
    version_id: UUID,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> AgentVersionResponse:
    version = await get_version_or_404(database, version_id)
    if (
        version.lifecycle_status != AgentVersionLifecycle.VERIFIED
        or version.preflight_status != PreflightStatus.PASSED
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "preflight_required", "message": "候选版本必须先通过预检"},
        )
    versions = list(
        await database.scalars(
            select(AgentVersion)
            .where(AgentVersion.agent_id == version.agent_id)
            .order_by(AgentVersion.version)
            .with_for_update()
        )
    )
    for item in versions:
        if item.lifecycle_status == AgentVersionLifecycle.ACTIVE:
            item.lifecycle_status = AgentVersionLifecycle.RETIRED
            item.enabled = False
    version.lifecycle_status = AgentVersionLifecycle.ACTIVE
    version.enabled = True
    await database.commit()
    await database.refresh(version)
    return version_response(version)


@router.post(
    "/agent-versions/{version_id}/disable",
    response_model=AgentVersionResponse,
)
async def disable_agent_version(
    version_id: UUID,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> AgentVersionResponse:
    version = await get_version_or_404(database, version_id)
    version.enabled = False
    version.lifecycle_status = AgentVersionLifecycle.RETIRED
    await database.commit()
    await database.refresh(version)
    return version_response(version)
