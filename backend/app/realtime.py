"""Authorized SSE invalidation, not an event log or a message transport."""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Message, Receive, Scope, Send

from app.auth.dependencies import load_session
from app.auth.security import SESSION_COOKIE_NAME, enforce_get_csrf_fetch_policy
from app.config import Settings
from app.errors import ApiError
from app.models import OutboxEvent, Role, Ticket, User
from app.outbox import CHANNEL

router = APIRouter(prefix="/api/v1", tags=["real-time"])
logger = logging.getLogger(__name__)
RESYNC = "resync"
OVERFLOW = "overflow"
AUTH_QUERY_TIMEOUT_SECONDS = 5.0
QueueItem = UUID | str


@dataclass(eq=False)
class Subscriber:
    user_id: UUID
    queue: asyncio.Queue[QueueItem]
    overflowed: bool = False


class EventHub:
    """One reconnecting LISTEN connection per process; bounded local subscribers."""

    def __init__(self, settings: Settings, factory: async_sessionmaker[AsyncSession]) -> None:
        self.settings = settings
        self.factory = factory
        self.incoming: asyncio.Queue[UUID] = asyncio.Queue(maxsize=256)
        self.subscribers: set[Subscriber] = set()
        self.ready = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.connection: Any = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.listen(), name="postgres-event-listener")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.ready.clear()

    def subscribe(self, user_id: UUID) -> Subscriber:
        if (
            sum(client.user_id == user_id for client in self.subscribers)
            >= self.settings.sse_max_per_user
        ):
            raise ApiError(429, "stream_limit", "Too many open streams for this account.")
        if len(self.subscribers) >= self.settings.sse_max_connections:
            raise ApiError(503, "stream_capacity", "Stream capacity reached. Retry later.")
        client = Subscriber(user_id, asyncio.Queue(maxsize=self.settings.sse_queue_size))
        self.subscribers.add(client)
        return client

    def unsubscribe(self, client: Subscriber) -> None:
        self.subscribers.discard(client)

    def enqueue(self, client: Subscriber, item: QueueItem) -> None:
        if client.overflowed:
            return
        if client.queue.full():
            while not client.queue.empty():
                client.queue.get_nowait()
            client.overflowed = True
            client.queue.put_nowait(OVERFLOW)
        else:
            client.queue.put_nowait(item)

    def broadcast(self, item: QueueItem) -> None:
        for client in tuple(self.subscribers):
            self.enqueue(client, item)

    def notified(self, _connection: Any, _pid: int, _channel: str, payload: str) -> None:
        try:
            event_id = UUID(payload)
        except ValueError:
            return
        # Global overload drops a wake-up; fixed-cadence reconciliation recovers
        # it. Private traffic must not trigger a customer's overflow or resync.
        if not self.incoming.full():
            self.incoming.put_nowait(event_id)

    async def fanout(self, event_id: UUID) -> None:
        async with asyncio.timeout(AUTH_QUERY_TIMEOUT_SECONDS), self.factory() as db:
            row = (
                await db.execute(
                    select(OutboxEvent.visibility, Ticket.customer_id)
                    .join(Ticket, Ticket.id == OutboxEvent.ticket_id)
                    .where(OutboxEvent.id == event_id)
                )
            ).one_or_none()
            if row is None:
                return
            audience = select(User.id).where(User.is_active.is_(True))
            if row.visibility == "PUBLIC":
                audience = audience.where(
                    (User.id == row.customer_id) | User.role.in_(["AGENT", "ADMIN"])
                )
            else:
                audience = audience.where(User.role.in_(["AGENT", "ADMIN"]))
            ids = {client.user_id for client in self.subscribers}
            if not ids:
                return
            allowed = set(await db.scalars(audience.where(User.id.in_(ids))))
        for client in tuple(self.subscribers):
            if client.user_id in allowed:
                self.enqueue(client, event_id)

    async def listen(self) -> None:
        dsn = self.settings.sqlalchemy_database_url.replace(
            "postgresql+asyncpg://", "postgresql://", 1
        )
        while True:
            connection = None
            try:
                connection = await asyncpg.connect(dsn, timeout=3, command_timeout=5)
                self.connection = connection
                await connection.add_listener(CHANNEL, self.notified)
                self.ready.set()
                self.broadcast(RESYNC)
                while not connection.is_closed():
                    try:
                        event_id = await asyncio.wait_for(self.incoming.get(), timeout=1)
                    except TimeoutError:
                        await connection.execute("SELECT 1")
                    else:
                        await self.fanout(event_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Event listener unavailable; reconnecting. Clients must reconcile.")
            finally:
                self.ready.clear()
                self.connection = None
                if connection is not None:
                    with contextlib.suppress(Exception):
                        await connection.close(timeout=2)
            await asyncio.sleep(1)


async def current_identity(db: AsyncSession, raw_token: str | None) -> User | None:
    session = await load_session(db, raw_token, require_user=True)
    if session is None or session.user_id is None:
        return None
    user = await db.get(User, session.user_id)
    if user is None or not user.is_active or user.role not in {role.value for role in Role}:
        return None
    return user


def frame(event: str, data: dict[str, str] | None = None) -> str:
    return f"event: {event}\ndata: {json.dumps(data or {}, separators=(',', ':'))}\n\n"


async def authorized_event(db: AsyncSession, user: User, event_id: UUID) -> UUID | None:
    query = (
        select(OutboxEvent.ticket_id)
        .join(Ticket, Ticket.id == OutboxEvent.ticket_id)
        .where(
            OutboxEvent.id == event_id,
        )
    )
    if user.role == Role.CUSTOMER:
        query = query.where(Ticket.customer_id == user.id, OutboxEvent.visibility == "PUBLIC")
    ticket_id: UUID | None = await db.scalar(query)
    return ticket_id


async def event_stream(
    request: Request,
    factory: async_sessionmaker[AsyncSession],
    hub: EventHub,
    subscriber: Subscriber,
    raw_token: str | None,
) -> AsyncIterator[str]:
    interval = hub.settings.sse_heartbeat_seconds
    loop = asyncio.get_running_loop()
    deadline = loop.time() + interval
    try:
        # Registered before the snapshot instruction: concurrent changes queue up.
        yield "retry: 2000\n" + frame("resync.required")
        while not await request.is_disconnected():
            item: QueueItem | None = None
            try:
                item = await asyncio.wait_for(
                    subscriber.queue.get(), max(0.001, deadline - loop.time())
                )
            except TimeoutError:
                pass
            async with asyncio.timeout(AUTH_QUERY_TIMEOUT_SECONDS), factory() as db:
                user = await current_identity(db, raw_token)
                if user is None:
                    # Close the transaction before yielding even the auth failure.
                    ticket_id = None
                else:
                    ticket_id = (
                        await authorized_event(db, user, item) if isinstance(item, UUID) else None
                    )
            if user is None:
                yield frame("auth.expired")
                return
            if item == OVERFLOW:
                yield frame("resync.required")
                return
            if item == RESYNC:
                yield frame("resync.required")
            if ticket_id is not None:
                yield frame("ticket.changed", {"ticket_id": str(ticket_id)})
                yield frame("queue.changed", {"ticket_id": str(ticket_id)})
            if loop.time() >= deadline:
                yield ": heartbeat\n\n"
                yield frame("resync.required")
                deadline = loop.time() + interval
    except asyncio.CancelledError:
        raise
    except Exception:
        # A DB failure closes rather than trusting cached authorization.
        logger.warning("Stream closed after dependency failure; reconnect and resync required.")
    finally:
        hub.unsubscribe(subscriber)


class BoundedStreamingResponse(StreamingResponse):
    send_timeout_seconds = 10.0
    cleanup: Callable[[], None]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def bounded_send(message: Message) -> None:
            try:
                await asyncio.wait_for(send(message), timeout=self.send_timeout_seconds)
            except TimeoutError as exc:
                raise OSError("Slow event-stream consumer") from exc

        try:
            await super().__call__(scope, receive, bounded_send)
        finally:
            self.cleanup()
            await cast(AsyncGenerator[str, None], self.body_iterator).aclose()


@router.get("/events", response_class=StreamingResponse)
async def events(request: Request) -> StreamingResponse:
    settings: Settings = request.app.state.settings
    enforce_get_csrf_fetch_policy(request, settings)
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    # No yield-based dependency: the initial authorization connection is released
    # before returning an indefinite response.
    try:
        async with asyncio.timeout(AUTH_QUERY_TIMEOUT_SECONDS), factory() as db:
            user = await current_identity(db, raw_token)
            if user is None:
                raise ApiError(401, "unauthenticated", "Authentication required.")
            user_id = user.id
    except TimeoutError as exc:
        raise ApiError(503, "stream_unavailable", "Live updates temporarily unavailable.") from exc
    hub: EventHub = request.app.state.event_hub
    try:
        await asyncio.wait_for(hub.ready.wait(), timeout=3)
    except TimeoutError as exc:
        raise ApiError(503, "stream_unavailable", "Live updates temporarily unavailable.") from exc
    subscriber = hub.subscribe(user_id)
    response = BoundedStreamingResponse(
        event_stream(request, factory, hub, subscriber, raw_token),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
    response.cleanup = lambda: hub.unsubscribe(subscriber)
    return response
