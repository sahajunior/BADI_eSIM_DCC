import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import ClientDisconnect
from starlette.types import Message

from app.config import Settings
from app.realtime import OVERFLOW, BoundedStreamingResponse, EventHub


def test_slow_subscriber_queue_is_bounded_and_requires_resync() -> None:
    engine = create_async_engine("postgresql+asyncpg://unused@localhost/test")
    hub = EventHub(Settings(sse_queue_size=2), async_sessionmaker(engine))
    subscriber = hub.subscribe(uuid4())
    for _ in range(100):
        hub.broadcast(uuid4())
    assert subscriber.overflowed
    assert subscriber.queue.qsize() == 1
    assert subscriber.queue.get_nowait() == OVERFLOW
    assert subscriber in hub.subscribers  # counts toward limits until actually disconnected
    hub.unsubscribe(subscriber)
    assert not hub.subscribers


async def test_slow_network_send_times_out_and_releases_subscription() -> None:
    cleaned_up: list[bool] = []

    async def body() -> AsyncIterator[str]:
        yield "event: resync.required\ndata: {}\n\n"

    async def send(_message: Message) -> None:
        await asyncio.sleep(10)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    response = BoundedStreamingResponse(body(), media_type="text/event-stream")
    response.cleanup = lambda: cleaned_up.append(True)
    response.send_timeout_seconds = 0.01
    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert cleaned_up == [True]


async def test_stalled_authorization_closes_stream_without_cached_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.requests import Request

    from app import realtime

    engine = create_async_engine("postgresql+asyncpg://unused@localhost/test")
    factory = async_sessionmaker(engine)
    hub = EventHub(Settings(sse_heartbeat_seconds=0.05), factory)
    subscriber = hub.subscribe(uuid4())

    async def stalled_identity(*_args: object) -> None:
        await asyncio.sleep(10)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    monkeypatch.setattr(realtime, "AUTH_QUERY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(realtime, "current_identity", stalled_identity)
    stream = realtime.event_stream(
        Request({"type": "http"}, receive), factory, hub, subscriber, None
    )
    assert "resync.required" in await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), 1)
    assert not hub.subscribers
    await engine.dispose()
