import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.attachments import router as attachments_router
from app.auth.routes import router as auth_router
from app.config import Settings, get_settings
from app.dashboard import router as dashboard_router
from app.db import READY_TIMEOUT_SECONDS, check_database_ready, create_engine, dispose_engine
from app.errors import install_error_handlers
from app.logging import configure_logging
from app.messages import router as messages_router
from app.notifications import router as notifications_router
from app.orders import router as orders_router
from app.realtime import EventHub
from app.realtime import router as realtime_router
from app.saved_replies import router as saved_replies_router
from app.tickets import router as tickets_router

request_logger = logging.getLogger("app.request")


class LiveResponse(BaseModel):
    status: Literal["ok"]


class ReadyResponse(BaseModel):
    status: Literal["ready"]
    database: Literal["ok"]


class UnavailableResponse(BaseModel):
    status: Literal["unavailable"]
    database: Literal["unavailable"]


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if resolved_settings.environment != "test":
            configure_logging(resolved_settings.log_level)
        engine = create_engine(resolved_settings)
        app.state.db_engine = engine
        app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
        hub = EventHub(resolved_settings, app.state.session_factory)
        app.state.event_hub = hub
        hub.start()
        try:
            yield
        finally:
            await hub.stop()
            await dispose_engine(engine)
            app.state.db_engine = None
            app.state.session_factory = None

    app = FastAPI(
        title="BADI Support Ticketing API",
        version="0.3.0",
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    install_error_handlers(app)
    app.include_router(auth_router)
    app.include_router(tickets_router)
    app.include_router(messages_router)
    app.include_router(realtime_router)
    app.include_router(orders_router)
    app.include_router(saved_replies_router)
    app.include_router(dashboard_router)
    app.include_router(attachments_router)
    app.include_router(notifications_router)

    @app.middleware("http")
    async def response_boundaries(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = str(uuid4())
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        if resolved_settings.environment != "test":
            # Path only: query strings may contain customer search terms.
            request_logger.info(
                "request",
                extra={
                    "request_id": request.state.request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
        return response

    @app.get("/api/v1/health/live", response_model=LiveResponse, tags=["health"])
    async def live() -> LiveResponse:
        return LiveResponse(status="ok")

    @app.get(
        "/api/v1/health/ready",
        response_model=ReadyResponse,
        responses={
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "model": UnavailableResponse,
                "description": "Database readiness check failed or timed out.",
            }
        },
        tags=["health"],
    )
    async def ready(request: Request) -> ReadyResponse | JSONResponse:
        engine = _get_engine(request.app.state)
        if engine is None:
            return _unavailable_response()
        try:
            await asyncio.wait_for(check_database_ready(engine), timeout=READY_TIMEOUT_SECONDS)
        except Exception:
            return _unavailable_response()
        return ReadyResponse(status="ready", database="ok")

    return app


def _get_engine(state: Any) -> AsyncEngine | None:
    engine = getattr(state, "db_engine", None)
    if isinstance(engine, AsyncEngine):
        return engine
    return None


def _unavailable_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=UnavailableResponse(status="unavailable", database="unavailable").model_dump(),
    )


app = create_app()
