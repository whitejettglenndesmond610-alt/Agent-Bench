import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient

from agent_eval.api.main import create_app
from agent_eval.core.artifact_store import LocalArtifactStore
from agent_eval.core.settings import Settings


async def test_health_endpoints(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    migration_config = Config("alembic.ini")
    migration_config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    await asyncio.to_thread(command.upgrade, migration_config, "head")
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{database_path}",
        environment="test",
    )

    artifact_store = LocalArtifactStore(tmp_path / "artifacts")
    app = create_app(settings, artifact_store)
    async with app.router.lifespan_context(app):
        assert app.state.artifact_store is artifact_store
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            live_response = await client.get("/health/live")
            ready_response = await client.get("/health/ready")
            metadata_response = await client.get("/api/v1/meta")

    assert live_response.json() == {"status": "ok"}
    assert ready_response.json() == {"status": "ready"}
    assert metadata_response.json()["environment"] == "test"
    assert (tmp_path / "artifacts").is_dir()
    assert database_path.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "artifacts").stat().st_mode & 0o777 == 0o700


async def test_readiness_rejects_unmigrated_database(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unmigrated.db'}",
        environment="test",
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


async def test_readiness_uses_injected_artifact_store(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    migration_config = Config("alembic.ini")
    migration_config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    await asyncio.to_thread(command.upgrade, migration_config, "head")
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{database_path}",
        environment="test",
    )

    class UnwritableStore(LocalArtifactStore):
        def verify_writable(self) -> None:
            raise OSError("artifact storage unavailable")

    artifact_store = UnwritableStore(tmp_path / "artifacts")
    app = create_app(settings, artifact_store)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
