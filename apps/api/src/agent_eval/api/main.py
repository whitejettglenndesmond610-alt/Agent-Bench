import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agent_eval import __version__
from agent_eval.api.routes.agents import router as agents_router
from agent_eval.api.routes.auth import router as auth_router
from agent_eval.api.routes.dataset_releases import router as dataset_releases_router
from agent_eval.api.routes.evaluation_cases import router as evaluation_cases_router
from agent_eval.api.routes.health import router as health_router
from agent_eval.api.routes.leaderboards import router as leaderboards_router
from agent_eval.api.routes.runs import router as runs_router
from agent_eval.api.routes.workspace import router as workspace_router
from agent_eval.api.routes.workspace_comparisons import router as workspace_comparisons_router
from agent_eval.auth.encryption import TokenCipher
from agent_eval.auth.rate_limit import LoginRateLimiter
from agent_eval.core.artifact_store import ArtifactStore, create_artifact_store
from agent_eval.core.logging import configure_logging
from agent_eval.core.settings import Settings
from agent_eval.core.storage import ensure_storage
from agent_eval.db.engine import create_engine
from agent_eval.db.session import create_session_factory
from agent_eval.worker.limits import WorkerLimits


def create_app(
    settings: Settings | None = None, artifact_store: ArtifactStore | None = None
) -> FastAPI:
    app_settings = settings or Settings()
    app_artifact_store = artifact_store or create_artifact_store(app_settings)
    app_settings.require_api_runtime()
    configure_logging(app_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ensure_storage(app_settings)
        engine = create_engine(app_settings)
        app.state.settings = app_settings
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        api_owner_id = f"api:{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
        app.state.agent_request_limit = WorkerLimits.from_settings(
            app_settings,
            engine=engine,
            owner_id=api_owner_id,
        ).agent_requests
        app.state.artifact_store = app_artifact_store
        app.state.login_rate_limiter = LoginRateLimiter(
            app_settings.login_max_attempts,
            app_settings.login_window_seconds,
        )
        app.state.token_cipher = (
            TokenCipher.from_secret(app_settings.encryption_key)
            if app_settings.encryption_key is not None
            else None
        )
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="Agent Evaluation Platform API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if app_settings.environment == "production" else "/api/docs",
        openapi_url=None if app_settings.environment == "production" else "/api/openapi.json",
    )

    @app.exception_handler(RequestValidationError)
    async def sanitized_validation_error(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "type": item.get("type", "value_error"),
                "loc": item.get("loc", ()),
                "msg": item.get("msg", "Invalid value"),
            }
            for item in error.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(agents_router)
    app.include_router(runs_router)
    app.include_router(leaderboards_router)
    app.include_router(workspace_router)
    app.include_router(workspace_comparisons_router)
    app.include_router(evaluation_cases_router)
    app.include_router(dataset_releases_router)
    return app


app = create_app()
