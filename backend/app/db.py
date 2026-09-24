from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import Settings

READY_TIMEOUT_SECONDS: Final[float] = 2.0
SCHEMA_REVISION: Final[str] = "0007_notifications"


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the async SQLAlchemy engine owned by FastAPI lifespan."""

    return create_async_engine(
        settings.sqlalchemy_database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


async def dispose_engine(engine: AsyncEngine | None) -> None:
    if engine is not None:
        await engine.dispose()


async def check_database_ready(engine: AsyncEngine) -> None:
    """Run a minimal bounded readiness query.

    Exceptions are intentionally allowed to bubble to the health route, where they are
    mapped to a generic unavailable response without leaking database details.
    """

    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
        result = await connection.execute(text("SELECT version_num FROM alembic_version"))
        if result.scalars().all() != [SCHEMA_REVISION]:
            raise RuntimeError("Database schema is not compatible with this application.")
