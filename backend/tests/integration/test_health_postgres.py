import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.main import create_app


@asynccontextmanager
async def lifespan_client(database_url: str) -> AsyncIterator[AsyncClient]:
    settings = Settings(database_url=database_url, environment="test")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
    assert app.state.db_engine is None


@pytest.mark.skipif(
    not os.environ.get("BADI_TEST_DATABASE_URL"),
    reason="BADI_TEST_DATABASE_URL is required for the real PostgreSQL integration test",
)
@pytest.mark.asyncio
async def test_readiness_checks_real_postgresql(migrated_database_url: str) -> None:
    async with lifespan_client(migrated_database_url) as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


@pytest.mark.parametrize("revision", ["unsupported_revision", None])
async def test_readiness_rejects_incompatible_schema(
    migrated_database_url: str, revision: str | None
) -> None:
    engine = create_async_engine(migrated_database_url)
    try:
        async with engine.begin() as connection:
            original = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            await connection.execute(text("DELETE FROM alembic_version"))
            if revision is not None:
                await connection.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                    {"revision": revision},
                )
        try:
            async with lifespan_client(migrated_database_url) as client:
                response = await client.get("/api/v1/health/ready")
                assert response.status_code == 503
                assert response.json() == {
                    "status": "unavailable",
                    "database": "unavailable",
                }
                assert (await client.get("/api/v1/health/live")).status_code == 200
        finally:
            async with engine.begin() as connection:
                await connection.execute(text("DELETE FROM alembic_version"))
                await connection.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                    {"revision": original},
                )
    finally:
        await engine.dispose()
