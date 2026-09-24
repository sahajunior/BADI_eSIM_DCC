from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from conftest import TEST_ORIGIN, TEST_PASSWORD, SeedData, _lifespan_client, session_for
from httpx import AsyncClient
from sqlalchemy import func, select, update

from app.models import IdempotencyRecord, OutboxEvent, Ticket, TicketEvent, TicketMessage


async def login(client: AsyncClient, email: str) -> str:
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": TEST_PASSWORD},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": csrf.json()["csrf_token"]},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


def write_headers(csrf_token: str, key: str = "message-key-1") -> dict[str, str]:
    return {"Origin": TEST_ORIGIN, "X-CSRF-Token": csrf_token, "Idempotency-Key": key}


async def post_message(
    client: AsyncClient,
    ticket_id: UUID,
    csrf_token: str,
    *,
    body: str = "Hello from the test suite",
    visibility: str | None = None,
    key: str = "message-key-1",
) -> Any:
    payload: dict[str, str] = {"body": body}
    if visibility is not None:
        payload["visibility"] = visibility
    return await client.post(
        f"/api/v1/tickets/{ticket_id}/messages",
        json=payload,
        headers=write_headers(csrf_token, key),
    )


async def set_ticket_status(database_url: str, ticket_id: UUID, status: str) -> None:
    values: dict[str, Any] = {"status": status, "updated_at": datetime.now(UTC)}
    if status == "RESOLVED":
        values["resolved_at"] = datetime.now(UTC)
    if status == "CLOSED":
        values["closed_at"] = datetime.now(UTC)
    async for session in session_for(database_url):
        await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(**values))
        await session.commit()


@asynccontextmanager
async def logged_in_client(database_url: str, email: str) -> AsyncIterator[tuple[AsyncClient, str]]:
    async with _lifespan_client(database_url) as client:
        token = await login(client, email)
        yield client, token


async def table_counts(database_url: str, ticket_id: UUID) -> dict[str, int]:
    async for session in session_for(database_url):
        message_count = await session.scalar(
            select(func.count())
            .select_from(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
        )
        event_count = await session.scalar(
            select(func.count()).select_from(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
        )
        outbox_count = await session.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.ticket_id == ticket_id)
        )
        idempotency_count = await session.scalar(
            select(func.count()).select_from(IdempotencyRecord)
        )
        return {
            "messages": int(message_count or 0),
            "events": int(event_count or 0),
            "outbox": int(outbox_count or 0),
            "idempotency": int(idempotency_count or 0),
        }
    raise AssertionError("session_for did not yield a session")


@pytest.mark.asyncio
async def test_customer_public_reply_creates_canonical_message_and_list_projection(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)

    create = await post_message(
        seeded_app_client,
        seed.tickets.customer_a_ticket_id,
        csrf,
        body="  My QR code still fails.  ",
        key="customer-public-create",
    )
    listed = await seeded_app_client.get(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}/messages"
    )

    assert create.status_code == 201, create.text
    created = create.json()
    assert created["body"] == "My QR code still fails."
    assert created["ticket_id"] == str(seed.tickets.customer_a_ticket_id)
    assert created["sender_type"] == "CUSTOMER"
    assert set(created) == {
        "id",
        "ticket_id",
        "body",
        "sender_type",
        "display_name",
        "created_at",
        "attachments",
    }
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == [created]


@pytest.mark.asyncio
async def test_agent_internal_note_is_visible_to_staff_and_hidden_from_customer(
    seeded_app_client: AsyncClient,
    clean_database: str,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    async with logged_in_client(clean_database, seed.users.agent_a_email) as (agent, agent_csrf):
        internal = await post_message(
            agent,
            seed.tickets.customer_a_ticket_id,
            agent_csrf,
            body="Provider escalation ID is private.",
            visibility="INTERNAL",
            key="agent-internal-note",
        )
        staff_list = await agent.get(
            f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}/messages"
        )

    customer_csrf = await login(seeded_app_client, seed.users.customer_a_email)
    customer_list = await seeded_app_client.get(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}/messages"
    )

    assert internal.status_code == 201, internal.text
    staff_item = staff_list.json()["items"][0]
    assert staff_item["visibility"] == "INTERNAL"
    assert staff_item["position"] == 1
    assert staff_item["sender_id"] == str(seed.users.agent_a_id)
    assert customer_csrf
    assert customer_list.status_code == 200, customer_list.text
    assert customer_list.json()["items"] == []
    assert "Provider escalation" not in customer_list.text


@pytest.mark.asyncio
async def test_create_message_rejects_forged_sender_fields(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)

    response = await seeded_app_client.post(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}/messages",
        json={
            "body": "Trying to impersonate support.",
            "sender_id": str(seed.users.agent_a_id),
            "sender_role_snapshot": "AGENT",
        },
        headers=write_headers(csrf, "forged-sender-fields"),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_list_messages_rejects_conflicting_before_and_after_markers(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)
    created = await post_message(
        seeded_app_client,
        seed.tickets.customer_a_ticket_id,
        csrf,
        body="Marker safety check.",
        key="marker-safety-message",
    )

    response = await seeded_app_client.get(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}/messages",
        params={"before": created.json()["id"], "after": created.json()["id"]},
    )

    assert created.status_code == 201, created.text
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_customer_cannot_create_internal_message(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)

    response = await post_message(
        seeded_app_client,
        seed.tickets.customer_a_ticket_id,
        csrf,
        body="Please hide this from me.",
        visibility="INTERNAL",
        key="customer-internal-forbidden",
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_customer_cannot_reply_to_another_customers_ticket(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)

    response = await post_message(
        seeded_app_client,
        seed.tickets.customer_b_ticket_id,
        csrf,
        body="Trying another customer's ticket.",
        key="other-customer-hidden",
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", "   "])
async def test_create_message_rejects_empty_or_whitespace_body(
    seeded_app_client: AsyncClient,
    body: str,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)

    response = await post_message(
        seeded_app_client,
        seed.tickets.customer_a_ticket_id,
        csrf,
        body=body,
        key=f"empty-body-{len(body)}",
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_message_rejects_body_over_10000_characters(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)

    response = await post_message(
        seeded_app_client,
        seed.tickets.customer_a_ticket_id,
        csrf,
        body="x" * 10001,
        key="too-long-body",
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_reusing_idempotency_key_with_same_body_replays_one_committed_write(
    seeded_app_client: AsyncClient,
    clean_database: str,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)

    first = await post_message(
        seeded_app_client,
        seed.tickets.customer_a_ticket_id,
        csrf,
        body="Please retry safely.",
        key="same-key-same-body",
    )
    replay = await post_message(
        seeded_app_client,
        seed.tickets.customer_a_ticket_id,
        csrf,
        body="Please retry safely.",
        key="same-key-same-body",
    )
    counts = await table_counts(clean_database, seed.tickets.customer_a_ticket_id)

    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["id"] == first.json()["id"]
    assert counts == {"messages": 1, "events": 1, "outbox": 1, "idempotency": 1}


@pytest.mark.asyncio
async def test_reusing_idempotency_key_with_different_body_conflicts_without_new_write(
    seeded_app_client: AsyncClient,
    clean_database: str,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)

    first = await post_message(
        seeded_app_client,
        seed.tickets.customer_a_ticket_id,
        csrf,
        body="Original body.",
        key="same-key-different-body",
    )
    conflict = await post_message(
        seeded_app_client,
        seed.tickets.customer_a_ticket_id,
        csrf,
        body="Changed body.",
        key="same-key-different-body",
    )
    counts = await table_counts(clean_database, seed.tickets.customer_a_ticket_id)

    assert first.status_code == 201, first.text
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert counts == {"messages": 1, "events": 1, "outbox": 1, "idempotency": 1}


@pytest.mark.asyncio
async def test_concurrent_same_key_replay_returns_one_message_and_one_side_effect_set(
    clean_database: str,
    seeded_data: SeedData,
) -> None:
    async with logged_in_client(clean_database, seeded_data.users.customer_a_email) as (
        first_client,
        first_csrf,
    ):
        async with logged_in_client(clean_database, seeded_data.users.customer_a_email) as (
            second_client,
            second_csrf,
        ):
            first, second = await asyncio.gather(
                post_message(
                    first_client,
                    seeded_data.tickets.customer_a_ticket_id,
                    first_csrf,
                    body="Submit once despite retry race.",
                    key="concurrent-same-key",
                ),
                post_message(
                    second_client,
                    seeded_data.tickets.customer_a_ticket_id,
                    second_csrf,
                    body="Submit once despite retry race.",
                    key="concurrent-same-key",
                ),
            )
    counts = await table_counts(clean_database, seeded_data.tickets.customer_a_ticket_id)

    assert [first.status_code, second.status_code].count(201) == 2
    assert first.json()["id"] == second.json()["id"]
    assert counts == {"messages": 1, "events": 1, "outbox": 1, "idempotency": 1}


@pytest.mark.asyncio
async def test_concurrent_distinct_messages_receive_gapless_ticket_positions(
    clean_database: str,
    seeded_data: SeedData,
) -> None:
    async with logged_in_client(clean_database, seeded_data.users.agent_a_email) as (
        first_client,
        first_csrf,
    ):
        async with logged_in_client(clean_database, seeded_data.users.agent_b_email) as (
            second_client,
            second_csrf,
        ):
            first, second = await asyncio.gather(
                post_message(
                    first_client,
                    seeded_data.tickets.customer_a_ticket_id,
                    first_csrf,
                    body="First concurrent public note.",
                    key="concurrent-position-a",
                ),
                post_message(
                    second_client,
                    seeded_data.tickets.customer_a_ticket_id,
                    second_csrf,
                    body="Second concurrent public note.",
                    key="concurrent-position-b",
                ),
            )
            listed = await first_client.get(
                f"/api/v1/tickets/{seeded_data.tickets.customer_a_ticket_id}/messages"
            )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert [item["position"] for item in listed.json()["items"]] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["WAITING_FOR_CUSTOMER", "RESOLVED"])
async def test_customer_public_reply_reopens_waiting_or_resolved_ticket_with_system_audit(
    seeded_app_client: AsyncClient,
    clean_database: str,
    initial_status: str,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await set_ticket_status(clean_database, seed.tickets.customer_a_ticket_id, initial_status)
    csrf = await login(seeded_app_client, seed.users.customer_a_email)

    response = await post_message(
        seeded_app_client,
        seed.tickets.customer_a_ticket_id,
        csrf,
        body="I have new information.",
        key=f"reopen-from-{initial_status.lower()}",
    )

    async for session in session_for(clean_database):
        ticket = await session.get(Ticket, seed.tickets.customer_a_ticket_id)
        event = await session.scalar(
            select(TicketEvent).where(
                TicketEvent.ticket_id == seed.tickets.customer_a_ticket_id,
                TicketEvent.event_type == "status.changed",
            )
        )
        assert ticket is not None
        assert event is not None
        assert response.status_code == 201, response.text
        assert ticket.status == "IN_PROGRESS"
        assert event.actor_id is None
        assert event.old_value == initial_status
        assert event.new_value == "IN_PROGRESS"
        assert event.reason == "customer_reply"


@pytest.mark.asyncio
async def test_closed_ticket_rejects_public_message_but_allows_internal_note(
    clean_database: str,
    seeded_data: SeedData,
) -> None:
    await set_ticket_status(clean_database, seeded_data.tickets.customer_a_ticket_id, "CLOSED")
    async with logged_in_client(clean_database, seeded_data.users.agent_a_email) as (agent, csrf):
        public = await post_message(
            agent,
            seeded_data.tickets.customer_a_ticket_id,
            csrf,
            body="Public message on closed ticket.",
            key="closed-public",
        )
        internal = await post_message(
            agent,
            seeded_data.tickets.customer_a_ticket_id,
            csrf,
            body="Private closure note.",
            visibility="INTERNAL",
            key="closed-internal",
        )

    assert public.status_code == 409
    assert public.json()["error"]["code"] == "ticket_closed"
    assert internal.status_code == 201, internal.text
    assert internal.json()["visibility"] == "INTERNAL"


@pytest.mark.asyncio
async def test_internal_note_does_not_change_public_updated_at(
    clean_database: str,
    seeded_data: SeedData,
) -> None:
    async for session in session_for(clean_database):
        ticket = await session.get(Ticket, seeded_data.tickets.customer_a_ticket_id)
        assert ticket is not None
        original_public_updated_at = ticket.public_updated_at

    async with logged_in_client(clean_database, seeded_data.users.agent_a_email) as (agent, csrf):
        response = await post_message(
            agent,
            seeded_data.tickets.customer_a_ticket_id,
            csrf,
            body="Private note only.",
            visibility="INTERNAL",
            key="public-updated-stable",
        )

    async for session in session_for(clean_database):
        ticket = await session.get(Ticket, seeded_data.tickets.customer_a_ticket_id)
        assert ticket is not None
        assert response.status_code == 201, response.text
        assert ticket.public_updated_at == original_public_updated_at


@pytest.mark.asyncio
async def test_customer_pagination_uses_opaque_public_markers_without_internal_gaps(
    clean_database: str,
    seeded_data: SeedData,
) -> None:
    async with logged_in_client(clean_database, seeded_data.users.agent_a_email) as (
        agent,
        agent_csrf,
    ):
        first_public = await post_message(
            agent,
            seeded_data.tickets.customer_a_ticket_id,
            agent_csrf,
            body="Public one.",
            key="marker-public-1",
        )
        internal = await post_message(
            agent,
            seeded_data.tickets.customer_a_ticket_id,
            agent_csrf,
            body="Internal gap.",
            visibility="INTERNAL",
            key="marker-internal",
        )
        second_public = await post_message(
            agent,
            seeded_data.tickets.customer_a_ticket_id,
            agent_csrf,
            body="Public two.",
            key="marker-public-2",
        )

    async with logged_in_client(clean_database, seeded_data.users.customer_a_email) as (
        customer,
        _,
    ):
        page = await customer.get(
            f"/api/v1/tickets/{seeded_data.tickets.customer_a_ticket_id}/messages",
            params={"limit": 1},
        )
        older = await customer.get(
            f"/api/v1/tickets/{seeded_data.tickets.customer_a_ticket_id}/messages",
            params={"before": page.json()["older_cursor"]},
        )
        forged = await customer.get(
            f"/api/v1/tickets/{seeded_data.tickets.customer_a_ticket_id}/messages",
            params={"before": internal.json()["id"]},
        )

    assert first_public.status_code == 201, first_public.text
    assert second_public.status_code == 201, second_public.text
    assert page.status_code == 200, page.text
    assert [item["body"] for item in page.json()["items"]] == ["Public two."]
    assert page.json()["has_more"] is True
    assert "position" not in page.text
    assert [item["body"] for item in older.json()["items"]] == ["Public one."]
    assert forged.status_code == 404
    assert "Internal gap" not in page.text + older.text + forged.text


@pytest.mark.asyncio
async def test_message_write_rolls_back_message_audit_outbox_and_idempotency_on_commit_failure(
    seeded_app_client: AsyncClient,
    clean_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)

    from sqlalchemy.ext.asyncio import AsyncSession

    async def fail_commit(self: AsyncSession) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    response = await post_message(
        seeded_app_client,
        seed.tickets.customer_a_ticket_id,
        csrf,
        body="This should roll back.",
        key="rollback-on-commit-failure",
    )
    counts = await table_counts(clean_database, seed.tickets.customer_a_ticket_id)

    assert response.status_code == 500
    assert counts == {"messages": 0, "events": 0, "outbox": 0, "idempotency": 0}


@pytest.mark.parametrize(
    "missing,expected", [("X-CSRF-Token", 403), ("Origin", 403), ("Idempotency-Key", 422)]
)
async def test_message_mutation_requires_csrf_origin_and_retry_key(
    seeded_app_client: AsyncClient, missing: str, expected: int
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)
    headers = write_headers(csrf)
    headers.pop(missing)
    response = await seeded_app_client.post(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}/messages",
        json={"body": "Rejected without the required write boundary"},
        headers=headers,
    )
    assert response.status_code == expected


async def test_idempotent_replay_reauthorizes_current_ticket_owner(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)
    first = await post_message(seeded_app_client, seed.tickets.customer_a_ticket_id, csrf)
    assert first.status_code == 201
    async for db in session_for(clean_database):
        await db.execute(
            update(Ticket)
            .where(Ticket.id == seed.tickets.customer_a_ticket_id)
            .values(customer_id=seed.users.customer_b_id)
        )
        await db.commit()
    replay = await post_message(seeded_app_client, seed.tickets.customer_a_ticket_id, csrf)
    assert replay.status_code == 404


async def test_public_reply_serializes_behind_close_transaction(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)
    async for db in session_for(clean_database):
        ticket = await db.scalar(
            select(Ticket).where(Ticket.id == seed.tickets.customer_a_ticket_id).with_for_update()
        )
        assert ticket is not None
        ticket.status = "CLOSED"
        ticket.closed_at = datetime.now(UTC)
        await db.flush()
        pending = asyncio.create_task(
            post_message(seeded_app_client, ticket.id, csrf, key="close-race")
        )
        try:
            await asyncio.sleep(0.05)
            assert not pending.done()
            await db.commit()
            response = await asyncio.wait_for(pending, timeout=3)
        finally:
            if not pending.done():
                pending.cancel()
        assert response.status_code == 409
    assert (await table_counts(clean_database, seed.tickets.customer_a_ticket_id))["messages"] == 0
