import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import app.main as main_module
from app.config import Settings
from app.main import create_app


@asynccontextmanager
async def client_with_engine(engine: AsyncEngine | None) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        database_url="postgresql+asyncpg://user:pass@localhost:5432/badi",
        environment="test",
    )
    app = create_app(settings)
    app.state.db_engine = engine
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.asyncio
async def test_liveness_does_not_require_database() -> None:
    async with client_with_engine(None) as client:
        response = await client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readiness_returns_503_without_database_engine() -> None:
    async with client_with_engine(None) as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}


@pytest.mark.asyncio
async def test_readiness_returns_503_without_leaking_connection_details() -> None:
    engine = create_async_engine("postgresql+asyncpg://wrong:secret@127.0.0.1:1/missing")
    try:
        async with client_with_engine(engine) as client:
            response = await client.get("/api/v1/health/ready")
    finally:
        await engine.dispose()

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}
    assert "secret" not in response.text
    assert "wrong" not in response.text
    assert "127.0.0.1" not in response.text


@pytest.mark.asyncio
async def test_readiness_timeout_returns_503_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_check(_engine: AsyncEngine) -> None:
        await asyncio.sleep(0.05)

    monkeypatch.setattr(main_module, "READY_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(
        main_module,
        "check_database_ready",
        slow_check,
    )
    engine = create_async_engine("postgresql+asyncpg://user:pass@localhost:5432/badi")
    try:
        async with client_with_engine(engine) as client:
            response = await client.get("/api/v1/health/ready")
    finally:
        await engine.dispose()

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}
    assert "pass" not in response.text


@pytest.mark.asyncio
async def test_docs_and_openapi_are_versioned() -> None:
    async with client_with_engine(None) as client:
        docs_response = await client.get("/api/v1/docs")
        openapi_response = await client.get("/api/v1/openapi.json")

    assert docs_response.status_code == 200
    assert openapi_response.status_code == 200
    assert openapi_response.json()["info"]["title"] == "BADI Support Ticketing API"


@pytest.mark.asyncio
async def test_openapi_documents_readiness_503_response() -> None:
    async with client_with_engine(None) as client:
        response = await client.get("/api/v1/openapi.json")

    responses = response.json()["paths"]["/api/v1/health/ready"]["get"]["responses"]
    assert "503" in responses
    assert responses["503"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/UnavailableResponse"
    )
