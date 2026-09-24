"""Phase 5 P0 matrix tests that were not already covered by phase-specific suites."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import SeedData, _lifespan_client, session_for
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_messages_phase3 import login, post_message

from app.models import OutboxEvent, Ticket
from app.outbox import backlog_stats, dispatch_batch


async def create_ticket(
    client: AsyncClient,
    csrf: str,
    email: str,
    *,
    subject: str,
    key: str,
    category: str = "OTHER",
    priority: str = "MEDIUM",
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/tickets",
        json={
            "customer_email": email,
            "category": category,
            "subject": subject,
            "description": f"Body for {subject}.",
            "priority": priority,
        },
        headers={
            "Origin": "http://127.0.0.1:8080",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": key,
        },
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def public_activity(database_url: str, ticket_id: UUID) -> datetime:
    async for session in session_for(database_url):
        ticket = await session.get(Ticket, ticket_id)
        assert ticket is not None
        return ticket.public_updated_at
    raise AssertionError("session_for did not yield a session")


@pytest.mark.asyncio
async def test_internal_notes_do_not_affect_customer_search_or_activity_ordering(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    """T09: internal notes never leak into customer search, order, or detail."""
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    seeded_ticket = seed.tickets.customer_a_ticket_id
    token = "PROVIDER-INTERNAL-TOKEN-9137"

    customer_csrf = await login(seeded_app_client, seed.users.customer_a_email)
    later = await create_ticket(
        seeded_app_client,
        customer_csrf,
        seed.users.customer_a_email,
        subject="Later public activity",
        key="later-public",
    )
    before_order = [
        item["id"]
        for item in (await seeded_app_client.get("/api/v1/tickets", params={"limit": 10})).json()[
            "items"
        ]
    ]
    assert before_order[0] == later["id"]
    assert str(seeded_ticket) in before_order

    async with _lifespan_client(clean_database) as agent:
        agent_csrf = await login(agent, seed.users.agent_a_email)
        note = await post_message(
            agent,
            seeded_ticket,
            agent_csrf,
            body=token,
            visibility="INTERNAL",
            key="internal-note-order",
        )
        assert note.status_code == 201, note.text

    after_order = [
        item["id"]
        for item in (await seeded_app_client.get("/api/v1/tickets", params={"limit": 10})).json()[
            "items"
        ]
    ]
    search = await seeded_app_client.get("/api/v1/tickets", params={"query": token})
    detail = await seeded_app_client.get(f"/api/v1/tickets/{seeded_ticket}")
    history = await seeded_app_client.get(f"/api/v1/tickets/{seeded_ticket}/public-history")
    conversation = await seeded_app_client.get(f"/api/v1/tickets/{seeded_ticket}/messages")

    assert after_order == before_order, "internal note changed customer activity ordering"
    assert search.status_code == 200
    assert search.json()["items"] == []
    assert token not in detail.text
    assert all("INTERNAL" not in item.get("new_value", "") for item in history.json()["items"])
    assert all(item["field"] == "status" for item in history.json()["items"])
    assert conversation.json()["items"] == []
    for internal_field in (
        "version",
        "updated_at",
        "assigned_agent_id",
        "customer_email_snapshot",
        "created_by_id",
        "first_agent_response_at",
    ):
        assert internal_field not in detail.json()


@pytest.mark.asyncio
async def test_ticket_paging_is_stable_with_equal_timestamps(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    """T13: keyset paging stays correct when sort values are identical."""
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.agent_a_email)

    shared = datetime.now(UTC)
    async for session in session_for(clean_database):
        await session.execute(update(Ticket).values(updated_at=shared))
        await session.commit()

    first_page = await seeded_app_client.get("/api/v1/tickets", params={"limit": 1})
    assert first_page.status_code == 200, first_page.text
    first_ids = [item["id"] for item in first_page.json()["items"]]
    cursor = first_page.json()["next_cursor"]
    assert len(first_ids) == 1
    assert cursor

    second_page = await seeded_app_client.get(
        "/api/v1/tickets", params={"limit": 1, "cursor": cursor}
    )
    assert second_page.status_code == 200, second_page.text
    second_ids = [item["id"] for item in second_page.json()["items"]]

    assert len(second_ids) == 1
    assert first_ids[0] != second_ids[0]
    assert set(first_ids + second_ids) == {
        str(seed.tickets.customer_a_ticket_id),
        str(seed.tickets.customer_b_ticket_id),
    }


@pytest.mark.asyncio
async def test_public_activity_timestamp_ignores_internal_writes(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    """T09: a customer-visible activity marker is not advanced by internal notes."""
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    before = await public_activity(clean_database, ticket_id)

    agent_csrf = await login(seeded_app_client, seed.users.agent_a_email)
    note = await post_message(
        seeded_app_client,
        ticket_id,
        agent_csrf,
        body="Internal only",
        visibility="INTERNAL",
        key="internal-activity",
    )
    assert note.status_code == 201, note.text
    after_internal = await public_activity(clean_database, ticket_id)

    reply = await post_message(
        seeded_app_client, ticket_id, agent_csrf, body="Public reply", key="public-activity"
    )
    assert reply.status_code == 201, reply.text
    after_public = await public_activity(clean_database, ticket_id)

    assert after_internal == before
    assert after_public > after_internal


@pytest.mark.asyncio
async def test_outbox_backlog_stats_report_pending_and_age(
    seeded_data: SeedData, clean_database: str
) -> None:
    engine = create_async_engine(clean_database)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            db.add(
                OutboxEvent(
                    ticket_id=seeded_data.tickets.customer_a_ticket_id,
                    kind="ticket.changed",
                    visibility="PUBLIC",
                    available_at=datetime.now(UTC) - timedelta(seconds=30),
                )
            )
            await db.commit()

        pending, oldest_seconds = await backlog_stats(factory)
        assert pending == 1
        assert oldest_seconds is not None and oldest_seconds >= 1

        assert await dispatch_batch(factory) == 1
        pending, oldest_seconds = await backlog_stats(factory)
        assert pending == 0
        assert oldest_seconds is None
    finally:
        await engine.dispose()
