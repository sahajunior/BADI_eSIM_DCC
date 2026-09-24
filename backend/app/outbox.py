"""Durable wake-ups: transactional PostgreSQL locks, NOTIFY and delivery markers."""

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import check_database_ready, create_engine
from app.logging import configure_logging
from app.models import OutboxEvent
from app.notifications import process_batch as process_notifications
from app.sla import record_breaches, utcnow

CHANNEL = "badi_ticket_changes"
MAX_ATTEMPTS = 8
logger = logging.getLogger(__name__)


async def publish(db: AsyncSession, event_id: UUID) -> None:
    # NOTIFY is delivered at commit. Its payload is only a pointer: listeners must
    # load committed metadata and reauthorize; no message body/email enters it.
    await db.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {
            "channel": CHANNEL,
            "payload": str(event_id),
        },
    )


async def dispatch_batch(factory: async_sessionmaker[AsyncSession], limit: int = 50) -> int:
    now = datetime.now(UTC)
    async with factory() as db, db.begin():
        events = list(
            await db.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.delivered_at.is_(None),
                    OutboxEvent.available_at <= now,
                    OutboxEvent.attempts < MAX_ATTEMPTS,
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for event in events:
            event.attempts += 1
            try:
                # A failed notification statement must not poison the outer
                # transaction that records the bounded retry/backoff.
                async with db.begin_nested():
                    await publish(db, event.id)
            except Exception:
                event.last_error = "notification_failed"
                event.available_at = now + timedelta(seconds=min(300, 2**event.attempts))
                logger.warning("Outbox notification failed; attempt=%s", event.attempts)
            else:
                event.delivered_at = datetime.now(UTC)
                event.last_error = None
        # Crash/commit failure rolls back both delivery markers and NOTIFY. There
        # is no external I/O between claim and ack, so transaction locks are leases.
        return len(events)


async def retry_failed(factory: async_sessionmaker[AsyncSession], event_id: UUID) -> bool:
    async with factory() as db, db.begin():
        event = await db.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.delivered_at.is_(None),
                OutboxEvent.attempts >= MAX_ATTEMPTS,
            )
            .with_for_update()
        )
        if event is None:
            return False
        event.attempts = 0
        event.available_at = datetime.now(UTC)
        event.last_error = None
        return True


async def record_sla_breaches(factory: async_sessionmaker[AsyncSession]) -> int:
    """Scan active SLA cycles and persist any newly breached timers."""
    async with factory() as db, db.begin():
        return len(await record_breaches(db, utcnow()))


async def backlog_stats(factory: async_sessionmaker[AsyncSession]) -> tuple[int, float | None]:
    """Undelivered event count and the age of the oldest pending event, in seconds."""
    now = datetime.now(UTC)
    async with factory() as db:
        pending = await db.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.delivered_at.is_(None))
        )
        oldest = await db.scalar(
            select(func.min(OutboxEvent.available_at)).where(OutboxEvent.delivered_at.is_(None))
        )
    if oldest is None:
        return int(pending or 0), None
    return int(pending or 0), max(0.0, (now - oldest).total_seconds())


async def run(*, once: bool = False, retry: UUID | None = None) -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await check_database_ready(engine)
        if retry is not None:
            if not await retry_failed(factory, retry):
                raise SystemExit("No exhausted undelivered event with that ID.")
            print("Outbox event scheduled for retry.")
            return
        loop = asyncio.get_running_loop()
        last_lag_log = 0.0
        while True:
            try:
                await record_sla_breaches(factory)
            except Exception:
                logger.warning("SLA breach scan failed; retrying without discarding state.")
            try:
                await process_notifications(factory, settings)
            except Exception:
                logger.warning("Notification delivery failed; retrying without discarding state.")
            try:
                count = await dispatch_batch(factory)
            except Exception:
                logger.warning("Outbox database unavailable; retrying without discarding events.")
                if once:
                    raise SystemExit(1) from None
                count = 0
            if loop.time() - last_lag_log >= settings.worker_lag_log_seconds:
                last_lag_log = loop.time()
                try:
                    pending, oldest_seconds = await backlog_stats(factory)
                    logger.info(
                        "outbox backlog",
                        extra={"pending": pending, "oldest_seconds": oldest_seconds},
                    )
                except Exception:
                    logger.warning("Outbox backlog statistics unavailable.")
            if once:
                return
            await asyncio.sleep(0.05 if count else 0.25)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Deliver committed ticket invalidations.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--retry", type=UUID, help="Explicitly retry one exhausted event UUID.")
    args = parser.parse_args()
    try:
        asyncio.run(run(once=args.once, retry=args.retry))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
