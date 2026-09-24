"""Real TCP streams with separate API listeners, no in-memory transport shortcut."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import uvicorn
from conftest import TEST_ORIGIN, SeedData, session_for
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_messages_phase3 import login, post_message

from app.auth.dependencies import hash_session_token
from app.config import Settings
from app.main import create_app
from app.models import AuthSession, OutboxEvent, User
from app.outbox import MAX_ATTEMPTS, dispatch_batch, retry_failed
from app.realtime import EventHub


@asynccontextmanager
async def live_api(database_url: str) -> AsyncIterator[tuple[AsyncClient, FastAPI]]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(32)
    port = sock.getsockname()[1]
    app = create_app(
        Settings(
            database_url=database_url,
            environment="test",
            allowed_origins=[TEST_ORIGIN],
            sse_heartbeat_seconds=0.15,
            sse_max_per_user=2,
        )
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            yield client, app
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        sock.close()


async def read_event(lines: AsyncIterator[str], expected: str) -> tuple[dict[str, Any], list[str]]:
    event = ""
    seen: list[str] = []
    async with asyncio.timeout(3):
        async for line in lines:
            seen.append(line)
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            if line.startswith("data: ") and event == expected:
                return json.loads(line.removeprefix("data: ")), seen
    raise AssertionError(f"Stream ended before {expected}: {seen}")


async def emit(app: FastAPI) -> int:
    return await dispatch_batch(app.state.session_factory)


async def test_cross_instance_notifications_privacy_and_resync(
    seeded_data: SeedData, clean_database: str
) -> None:
    ticket_id = seeded_data.tickets.customer_a_ticket_id
    async with (
        live_api(clean_database) as (customer, customer_app),
        live_api(clean_database) as (agent, agent_app),
    ):
        customer_csrf = await login(customer, seeded_data.users.customer_a_email)
        agent_csrf = await login(agent, seeded_data.users.agent_a_email)
        async with (
            customer.stream("GET", "/api/v1/events") as public_feed,
            agent.stream("GET", "/api/v1/events") as staff_feed,
        ):
            assert public_feed.status_code == staff_feed.status_code == 200
            assert public_feed.headers["content-type"].startswith("text/event-stream")
            assert public_feed.headers["x-accel-buffering"] == "no"
            public_lines, staff_lines = public_feed.aiter_lines(), staff_feed.aiter_lines()
            await read_event(public_lines, "resync.required")
            await read_event(staff_lines, "resync.required")
            # No worker runs inside either API. Pending rows remain durable.
            created = await post_message(
                agent, ticket_id, agent_csrf, body="Public answer", key="public-answer"
            )
            assert created.status_code == 201
            assert await emit(agent_app) == 1
            event, seen = await read_event(public_lines, "ticket.changed")
            assert event == {"ticket_id": str(ticket_id)}
            assert not any(line.startswith("id:") for line in seen)
            await read_event(staff_lines, "ticket.changed")
            await read_event(public_lines, "queue.changed")
            await read_event(staff_lines, "queue.changed")
            internal = await post_message(
                agent,
                ticket_id,
                agent_csrf,
                body="PRIVATE PROVIDER SECRET",
                visibility="INTERNAL",
                key="internal",
            )
            assert internal.status_code == 201
            assert await emit(agent_app) == 1
            await read_event(staff_lines, "ticket.changed")
            # Fixed-time reconciliation is independent of hidden traffic.
            _, public_seen = await read_event(public_lines, "resync.required")
            assert "ticket.changed" not in "\n".join(public_seen)
            assert "PRIVATE" not in "\n".join(public_seen)
            reply = await post_message(
                customer, ticket_id, customer_csrf, body="It works now", key="reply"
            )
            assert reply.status_code == 201
            assert await emit(customer_app) == 1
            await read_event(staff_lines, "ticket.changed")
            assert (
                customer_app.state.event_hub.connection is not agent_app.state.event_hub.connection
            )
        # Connection cleanup does not retain pool connections or subscriber slots.
        async with asyncio.timeout(2):
            while customer_app.state.event_hub.subscribers or agent_app.state.event_hub.subscribers:
                await asyncio.sleep(0.01)


async def test_other_customer_feed_cannot_observe_ticket_invalidations(
    seeded_data: SeedData, clean_database: str
) -> None:
    async with (
        live_api(clean_database) as (customer_b, app),
        live_api(clean_database) as (agent, _),
    ):
        await login(customer_b, seeded_data.users.customer_b_email)
        csrf = await login(agent, seeded_data.users.agent_a_email)
        async with customer_b.stream("GET", "/api/v1/events") as feed:
            lines = feed.aiter_lines()
            await read_event(lines, "resync.required")
            response = await post_message(
                agent, seeded_data.tickets.customer_a_ticket_id, csrf, key="other-ticket"
            )
            assert response.status_code == 201
            await emit(app)
            _, seen = await read_event(lines, "resync.required")
            assert not any(
                "ticket.changed" in line or str(seeded_data.tickets.customer_a_ticket_id) in line
                for line in seen
            )


@pytest.mark.parametrize("change", ["revoke", "expire", "deactivate"])
async def test_open_stream_revalidates_identity_at_heartbeat(
    seeded_data: SeedData, clean_database: str, change: str
) -> None:
    async with live_api(clean_database) as (client, _):
        await login(client, seeded_data.users.customer_a_email)
        raw_token = client.cookies["badi_session"]
        async with client.stream("GET", "/api/v1/events") as feed:
            lines = feed.aiter_lines()
            await read_event(lines, "resync.required")
            async for db in session_for(clean_database):
                if change == "deactivate":
                    await db.execute(
                        update(User)
                        .where(User.id == seeded_data.users.customer_a_id)
                        .values(is_active=False)
                    )
                else:
                    values = (
                        {"revoked_at": datetime.now(UTC)}
                        if change == "revoke"
                        else {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
                    )
                    await db.execute(
                        update(AuthSession)
                        .where(AuthSession.token_hash == hash_session_token(raw_token))
                        .values(**values)
                    )
                await db.commit()
            await read_event(lines, "auth.expired")
            async with asyncio.timeout(2):
                assert [line async for line in lines] == [""]


async def test_stream_auth_origin_limits_and_disconnect_cleanup(
    seeded_data: SeedData, clean_database: str
) -> None:
    async with live_api(clean_database) as (client, app):
        assert (await client.get("/api/v1/events")).status_code == 401
        await login(client, seeded_data.users.customer_a_email)
        assert (
            await client.get("/api/v1/events", headers={"Origin": "https://evil.example"})
        ).status_code == 403
        async with (
            client.stream("GET", "/api/v1/events") as first,
            client.stream("GET", "/api/v1/events") as second,
        ):
            assert first.status_code == second.status_code == 200
            assert (await client.get("/api/v1/events")).status_code == 429
        async with asyncio.timeout(2):
            while app.state.event_hub.subscribers:
                await asyncio.sleep(0.01)
        async with client.stream("GET", "/api/v1/events") as replacement:
            assert replacement.status_code == 200


async def test_restart_recovers_canonical_messages_with_fresh_resync(
    seeded_data: SeedData, clean_database: str
) -> None:
    ticket_id = seeded_data.tickets.customer_a_ticket_id
    async with live_api(clean_database) as (first, _):
        csrf = await login(first, seeded_data.users.customer_a_email)
        token = first.cookies["badi_session"]
        result = await post_message(
            first, ticket_id, csrf, body="Persisted across restart", key="restart"
        )
        assert result.status_code == 201
    async with live_api(clean_database) as (second, app):
        second.cookies.set("badi_session", token)
        async with second.stream(
            "GET", "/api/v1/events", headers={"Last-Event-ID": "not-a-replay-cursor"}
        ) as feed:
            data, _ = await read_event(feed.aiter_lines(), "resync.required")
            assert data == {}
            messages = await second.get(f"/api/v1/tickets/{ticket_id}/messages")
            assert [row["id"] for row in messages.json()["items"]] == [result.json()["id"]]
            assert await emit(app) == 1


async def test_listener_reconnect_and_duplicate_notifications_are_safe(
    seeded_data: SeedData, clean_database: str
) -> None:
    async with live_api(clean_database) as (client, app):
        csrf = await login(client, seeded_data.users.customer_a_email)
        async with client.stream("GET", "/api/v1/events") as feed:
            lines = feed.aiter_lines()
            await read_event(lines, "resync.required")
            hub: EventHub = app.state.event_hub
            old_connection = hub.connection
            await old_connection.close()
            async with asyncio.timeout(4):
                while hub.connection is None or hub.connection is old_connection:
                    await asyncio.sleep(0.01)
            created = await post_message(
                client, seeded_data.tickets.customer_a_ticket_id, csrf, key="after-reconnect"
            )
            assert created.status_code == 201
            await emit(app)
            await read_event(lines, "ticket.changed")
            async with app.state.session_factory() as db:
                event = await db.scalar(select(OutboxEvent))
                assert event is not None
                await hub.fanout(event.id)
                await hub.fanout(event.id)
            await read_event(lines, "ticket.changed")
            listed = await client.get(
                f"/api/v1/tickets/{seeded_data.tickets.customer_a_ticket_id}/messages"
            )
            assert len(listed.json()["items"]) == 1


async def test_worker_failure_backoff_exhaustion_manual_retry_and_concurrent_workers(
    seeded_data: SeedData, clean_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.outbox as worker

    engine = create_async_engine(clean_database)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            event = OutboxEvent(
                ticket_id=seeded_data.tickets.customer_a_ticket_id,
                kind="ticket.changed",
                visibility="PUBLIC",
            )
            db.add(event)
            await db.commit()
            event_id = event.id

        async def fail_publish(_db: Any, _id: UUID) -> None:
            raise RuntimeError("private diagnostic must not be persisted")

        with monkeypatch.context() as patch:
            patch.setattr(worker, "publish", fail_publish)
            assert await dispatch_batch(factory) == 1
        async with factory() as db:
            failed = await db.get(OutboxEvent, event_id)
            assert failed is not None
            assert failed.attempts == 1 and failed.delivered_at is None
            assert failed.last_error == "notification_failed"
            assert failed.available_at > datetime.now(UTC)
            failed.attempts = MAX_ATTEMPTS
            failed.available_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        assert await dispatch_batch(factory) == 0
        assert await retry_failed(factory, event_id)
        counts = await asyncio.gather(dispatch_batch(factory), dispatch_batch(factory))
        assert sum(counts) == 1
        async with factory() as db:
            delivered = await db.get(OutboxEvent, event_id)
            assert delivered is not None and delivered.delivered_at is not None
            assert delivered.attempts == 1 and delivered.last_error is None
        assert not await retry_failed(factory, event_id)
    finally:
        await engine.dispose()
