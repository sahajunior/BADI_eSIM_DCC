"""Delete staged-but-unattached attachment objects past their expiry."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.attachments import delete_expired
from app.config import Settings
from app.db import create_engine
from app.logging import configure_logging
from app.storage import storage_for
from app.unread import utcnow


async def _run() -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = storage_for(settings)
    try:
        async with factory() as db, db.begin():
            removed = await delete_expired(db, store, utcnow())
        print(f"attachment cleanup complete: removed={removed}")
        return removed
    finally:
        await engine.dispose()


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
