from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_eval.api.dependencies import DatabaseSession, WorkspaceCsrf, WorkspaceSession
from agent_eval.api.mutation_guard import sqlite_mutation_guard
from agent_eval.api.routes.workspace import workspace_error, workspace_user_id
from agent_eval.api.schemas.workspace_personalization import (
    WorkspacePromptTemplateCreate,
    WorkspacePromptTemplateDelete,
    WorkspacePromptTemplateList,
    WorkspacePromptTemplatePatch,
    WorkspacePromptTemplateResponse,
)
from agent_eval.db.base import utc_now
from agent_eval.db.enums import AgentStatus, AgentVersionLifecycle
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    WorkspaceAgentFavorite,
    WorkspacePromptTemplate,
)

router = APIRouter(prefix="/api/v1/workspace", tags=["workspace-personalization"])


def template_response(template: WorkspacePromptTemplate) -> WorkspacePromptTemplateResponse:
    return WorkspacePromptTemplateResponse(
        id=template.id,
        name=template.name,
        content=template.content,
        revision=template.revision,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


async def owned_template(
    database: AsyncSession,
    template_id: UUID,
    owner_user_id: UUID,
    *,
    lock: bool = False,
) -> WorkspacePromptTemplate:
    statement = select(WorkspacePromptTemplate).where(
        WorkspacePromptTemplate.id == template_id,
        WorkspacePromptTemplate.owner_user_id == owner_user_id,
    )
    if lock:
        statement = statement.with_for_update(of=WorkspacePromptTemplate)
    template = await database.scalar(statement)
    if template is None:
        raise workspace_error(404, "prompt_template_not_found", "Prompt template not found")
    return template


@router.put("/agents/{agent_id}/favorite", status_code=status.HTTP_204_NO_CONTENT)
async def favorite_agent(
    agent_id: UUID,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> Response:
    owner_user_id = workspace_user_id(auth_session)
    async with sqlite_mutation_guard(
        request, database, f"workspace-favorite:{owner_user_id}:{agent_id}"
    ):
        visible_agent_id = await database.scalar(
            select(Agent.id)
            .join(AgentVersion, AgentVersion.agent_id == Agent.id)
            .where(
                Agent.id == agent_id,
                Agent.status == AgentStatus.ENABLED,
                AgentVersion.enabled.is_(True),
                AgentVersion.lifecycle_status == AgentVersionLifecycle.ACTIVE,
            )
            .limit(1)
        )
        if visible_agent_id is None:
            raise workspace_error(404, "agent_not_found", "Agent not found")
        favorite = await database.get(
            WorkspaceAgentFavorite,
            {"owner_user_id": owner_user_id, "agent_id": agent_id},
        )
        if favorite is None:
            database.add(
                WorkspaceAgentFavorite(
                    owner_user_id=owner_user_id,
                    agent_id=agent_id,
                )
            )
            try:
                await database.commit()
            except IntegrityError as error:
                await database.rollback()
                favorite = await database.get(
                    WorkspaceAgentFavorite,
                    {"owner_user_id": owner_user_id, "agent_id": agent_id},
                )
                if favorite is None:
                    raise workspace_error(
                        409, "favorite_conflict", "Favorite changed concurrently"
                    ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/agents/{agent_id}/favorite", status_code=status.HTTP_204_NO_CONTENT)
async def unfavorite_agent(
    agent_id: UUID,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> Response:
    await database.execute(
        delete(WorkspaceAgentFavorite).where(
            WorkspaceAgentFavorite.owner_user_id == workspace_user_id(auth_session),
            WorkspaceAgentFavorite.agent_id == agent_id,
        )
    )
    await database.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/prompt-templates", response_model=WorkspacePromptTemplateList)
async def list_prompt_templates(
    response: Response,
    database: DatabaseSession,
    auth_session: WorkspaceSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> WorkspacePromptTemplateList:
    templates = list(
        await database.scalars(
            select(WorkspacePromptTemplate)
            .where(
                WorkspacePromptTemplate.owner_user_id == workspace_user_id(auth_session)
            )
            .order_by(
                WorkspacePromptTemplate.updated_at.desc(),
                WorkspacePromptTemplate.id.desc(),
            )
            .limit(limit)
        )
    )
    response.headers["Cache-Control"] = "private, no-store"
    return WorkspacePromptTemplateList(items=[template_response(item) for item in templates])


@router.post(
    "/prompt-templates",
    response_model=WorkspacePromptTemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_prompt_template(
    payload: WorkspacePromptTemplateCreate,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> WorkspacePromptTemplateResponse:
    owner_user_id = workspace_user_id(auth_session)
    async with sqlite_mutation_guard(
        request, database, f"workspace-template-create:{owner_user_id}"
    ):
        template = WorkspacePromptTemplate(
            owner_user_id=owner_user_id,
            name=payload.name,
            content=payload.content,
        )
        database.add(template)
        try:
            await database.commit()
        except IntegrityError as error:
            await database.rollback()
            duplicate = await database.scalar(
                select(WorkspacePromptTemplate.id).where(
                    WorkspacePromptTemplate.owner_user_id == owner_user_id,
                    WorkspacePromptTemplate.name == payload.name,
                )
            )
            if duplicate is not None:
                raise workspace_error(
                    409,
                    "prompt_template_name_conflict",
                    "A prompt template with this name already exists",
                ) from error
            raise workspace_error(
                409, "prompt_template_conflict", "Prompt template changed concurrently"
            ) from error
        return template_response(template)


@router.get(
    "/prompt-templates/{template_id}", response_model=WorkspacePromptTemplateResponse
)
async def get_prompt_template(
    template_id: UUID,
    response: Response,
    database: DatabaseSession,
    auth_session: WorkspaceSession,
) -> WorkspacePromptTemplateResponse:
    template = await owned_template(
        database, template_id, workspace_user_id(auth_session)
    )
    response.headers["Cache-Control"] = "private, no-store"
    return template_response(template)


@router.patch(
    "/prompt-templates/{template_id}", response_model=WorkspacePromptTemplateResponse
)
async def patch_prompt_template(
    template_id: UUID,
    payload: WorkspacePromptTemplatePatch,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> WorkspacePromptTemplateResponse:
    owner_user_id = workspace_user_id(auth_session)
    async with sqlite_mutation_guard(
        request, database, f"workspace-template:{template_id}"
    ):
        template = await owned_template(
            database, template_id, owner_user_id, lock=True
        )
        if template.revision != payload.expected_revision:
            raise workspace_error(409, "revision_conflict", "Prompt template revision changed")
        if "name" in payload.model_fields_set:
            assert payload.name is not None
            template.name = payload.name
        if "content" in payload.model_fields_set:
            assert payload.content is not None
            template.content = payload.content
        template.revision += 1
        template.updated_at = utc_now()
        try:
            await database.commit()
        except IntegrityError as error:
            await database.rollback()
            duplicate = await database.scalar(
                select(WorkspacePromptTemplate.id).where(
                    WorkspacePromptTemplate.owner_user_id == owner_user_id,
                    WorkspacePromptTemplate.name == payload.name,
                    WorkspacePromptTemplate.id != template_id,
                )
            )
            if duplicate is not None:
                raise workspace_error(
                    409,
                    "prompt_template_name_conflict",
                    "A prompt template with this name already exists",
                ) from error
            raise workspace_error(
                409, "prompt_template_conflict", "Prompt template changed concurrently"
            ) from error
        return template_response(template)


@router.delete("/prompt-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt_template(
    template_id: UUID,
    payload: WorkspacePromptTemplateDelete,
    request: Request,
    database: DatabaseSession,
    auth_session: WorkspaceCsrf,
) -> Response:
    owner_user_id = workspace_user_id(auth_session)
    async with sqlite_mutation_guard(
        request, database, f"workspace-template:{template_id}"
    ):
        template = await owned_template(
            database,
            template_id,
            owner_user_id,
            lock=True,
        )
        if template.revision != payload.expected_revision:
            raise workspace_error(409, "revision_conflict", "Prompt template revision changed")
        await database.delete(template)
        await database.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
