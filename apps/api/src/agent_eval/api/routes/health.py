import asyncio

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_eval import __version__
from agent_eval.core.artifact_store import ArtifactStore
from agent_eval.core.settings import Settings
from agent_eval.db.engine import verify_database

router = APIRouter()


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    engine: AsyncEngine = request.app.state.engine
    artifact_store: ArtifactStore = request.app.state.artifact_store

    try:
        await verify_database(engine)
        await asyncio.to_thread(artifact_store.verify_writable)
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable"},
        )

    return JSONResponse(content={"status": "ready"})


@router.get("/api/v1/meta")
async def metadata(request: Request) -> dict[str, str]:
    settings: Settings = request.app.state.settings
    return {
        "name": "Agent Evaluation Platform",
        "version": __version__,
        "environment": settings.environment,
        "status": "ready",
    }
