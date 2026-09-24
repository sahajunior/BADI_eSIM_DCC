from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from conftest import TEST_ORIGIN, TEST_PASSWORD, SeedData, _lifespan_client, session_for
from httpx import AsyncClient
from sqlalchemy import func, select, text

from app.auth.passwords import hash_password
from app.models import IdempotencyRecord, OutboxEvent, Role, Ticket, TicketEvent, User


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


def write_headers(csrf: str, key: str | None = None, etag: str | None = None) -> dict[str, str]:
    headers = {"Origin": TEST_ORIGIN, "X-CSRF-Token": csrf}
    if key is not None:
        headers["Idempotency-Key"] = key
    if etag is not None:
        headers["If-Match"] = etag
    return headers


async def post_public_staff_reply(client: AsyncClient, ticket_id: UUID, csrf: str) -> None:
    response = await client.post(
        f"/api/v1/tickets/{ticket_id}/messages",
        json={"body": "Public resolution explanation."},
        headers=write_headers(csrf, "public-resolution-reply"),
    )
    assert response.status_code == 201, response.text


async def count_side_effects(database_url: str, ticket_id: UUID) -> dict[str, int]:
    async for session in session_for(database_url):
        return {
            "events": int(
                await session.scalar(
                    select(func.count())
                    .select_from(TicketEvent)
                    .where(TicketEvent.ticket_id == ticket_id)
                )
                or 0
            ),
            "outbox": int(
                await session.scalar(
                    select(func.count())
                    .select_from(OutboxEvent)
                    .where(OutboxEvent.ticket_id == ticket_id)
                )
                or 0
            ),
            "idem": int(
                await session.scalar(select(func.count()).select_from(IdempotencyRecord)) or 0
            ),
        }
    raise AssertionError("session unavailable")


@pytest.mark.asyncio
async def test_customer_create_list_detail_and_idempotent_replay(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)
    payload = {
        "customer_email": seed.users.customer_a_email,
        "category": "CONNECTIVITY",
        "subject": "  New eSIM data problem  ",
        "description": "  Data stops after activation.  ",
        "priority": "HIGH",
    }

    first = await seeded_app_client.post(
        "/api/v1/tickets", json=payload, headers=write_headers(csrf, "create-ticket-a")
    )
    replay = await seeded_app_client.post(
        "/api/v1/tickets", json=payload, headers=write_headers(csrf, "create-ticket-a")
    )
    listed = await seeded_app_client.get("/api/v1/tickets", params={"query": "data problem"})
    detail = await seeded_app_client.get(f"/api/v1/tickets/{first.json()['id']}")

    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["id"] == first.json()["id"]
    assert first.json()["status"] == "OPEN"
    assert (
        first.json()["assigned_agent_id"] is None if "assigned_agent_id" in first.json() else True
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [first.json()["id"]]
    assert detail.status_code == 200
    assert "ETag" not in detail.headers


@pytest.mark.asyncio
async def test_customer_cannot_mass_assign_or_create_for_other_customer(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)

    response = await seeded_app_client.post(
        "/api/v1/tickets",
        json={
            "customer_email": seed.users.customer_b_email,
            "customer_id": str(seed.users.customer_b_id),
            "category": "OTHER",
            "subject": "Bad owner",
            "description": "Attempt owner selection.",
            "priority": "LOW",
        },
        headers=write_headers(csrf, "mass-owner"),
    )
    staff_filter = await seeded_app_client.get("/api/v1/tickets", params={"unassigned": "true"})

    assert response.status_code == 403
    assert staff_filter.status_code == 403


@pytest.mark.asyncio
async def test_staff_on_behalf_customer_lookup_filters_and_cursor(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    lookup = await seeded_app_client.get(
        "/api/v1/customers", params={"query": "customer", "limit": 1}
    )
    create = await seeded_app_client.post(
        "/api/v1/tickets",
        json={
            "customer_email": seed.users.customer_b_email,
            "customer_id": str(seed.users.customer_b_id),
            "category": "ORDER",
            "subject": "Order support",
            "description": "Created by support on behalf of customer.",
            "priority": "MEDIUM",
        },
        headers=write_headers(csrf, "agent-create-on-behalf"),
    )
    assigned = await seeded_app_client.get(
        "/api/v1/tickets", params={"assigned_agent_id": str(seed.users.agent_a_id)}
    )
    unassigned = await seeded_app_client.get("/api/v1/tickets", params={"unassigned": "true"})

    assert lookup.status_code == 200, lookup.text
    assert len(lookup.json()["items"]) == 1
    assert lookup.json()["next_cursor"]
    assert create.status_code == 201, create.text
    assert create.json()["customer_id"] == str(seed.users.customer_b_id)
    assert create.headers["etag"] == f'"{create.json()["id"]}:1"'
    assert all(
        item["assigned_agent_id"] == str(seed.users.agent_a_id) for item in assigned.json()["items"]
    )
    assert create.json()["id"] in [item["id"] for item in unassigned.json()["items"]]


@pytest.mark.asyncio
async def test_staff_patch_etag_noop_transition_reply_gate_and_audit(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.agent_a_email)
    detail = await seeded_app_client.get(f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}")
    etag = detail.headers["etag"]
    before = await count_side_effects(clean_database, seed.tickets.customer_a_ticket_id)

    missing = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={"priority": "URGENT"},
        headers=write_headers(csrf),
    )
    stale = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={"priority": "URGENT"},
        headers=write_headers(csrf, etag='"stale:1"'),
    )
    blocked_resolve = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={"status": "RESOLVED"},
        headers=write_headers(csrf, etag=etag),
    )
    noop = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={
            "priority": detail.json()["priority"],
            "assigned_agent_id": detail.json()["assigned_agent_id"],
        },
        headers=write_headers(csrf, etag=etag),
    )
    # Rejected/no-op requests above must add no events, outbox rows, or idempotency records.
    after_rejections = await count_side_effects(clean_database, seed.tickets.customer_a_ticket_id)
    await post_public_staff_reply(seeded_app_client, seed.tickets.customer_a_ticket_id, csrf)
    fresh = await seeded_app_client.get(f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}")
    patched = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={
            "status": "RESOLVED",
            "priority": "URGENT",
            "assigned_agent_id": None,
            "reason": "Customer confirmed fix.",
        },
        headers=write_headers(csrf, etag=fresh.headers["etag"]),
    )
    after_success = await count_side_effects(clean_database, seed.tickets.customer_a_ticket_id)
    events = await seeded_app_client.get(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}/events"
    )
    history = await seeded_app_client.get(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}/public-history"
    )

    assert missing.status_code == 428
    assert stale.status_code == 412
    assert blocked_resolve.status_code == 409
    assert noop.status_code == 200
    assert before == after_rejections
    assert after_success["events"] > after_rejections["events"]
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "RESOLVED"
    assert patched.json()["priority"] == "URGENT"
    assert patched.json()["assigned_agent_id"] is None
    fields = {item["field"] for item in events.json()["items"]}
    assert {"status", "priority", "assigned_agent_id"} <= fields
    assert all("request_id" not in item for item in events.json()["items"])
    assert all(item["field"] == "status" for item in history.json()["items"])
    assert all("actor_id" not in item and "reason" not in item for item in history.json()["items"])


@pytest.mark.asyncio
async def test_reopen_closed_requires_reason(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.agent_a_email)
    await post_public_staff_reply(seeded_app_client, seed.tickets.customer_a_ticket_id, csrf)
    detail = await seeded_app_client.get(f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}")
    resolved = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={"status": "RESOLVED"},
        headers=write_headers(csrf, etag=detail.headers["etag"]),
    )
    closed = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={"status": "CLOSED"},
        headers=write_headers(csrf, etag=resolved.headers["etag"]),
    )

    no_reason = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={"status": "IN_PROGRESS"},
        headers=write_headers(csrf, etag=closed.headers["etag"]),
    )
    with_reason = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={"status": "IN_PROGRESS", "reason": "Customer reopened with new evidence."},
        headers=write_headers(csrf, etag=closed.headers["etag"]),
    )

    assert closed.status_code == 200
    assert no_reason.status_code == 422
    assert with_reason.status_code == 200, with_reason.text
    async for session in session_for(clean_database):
        ticket = await session.get(Ticket, seed.tickets.customer_a_ticket_id)
        assert ticket is not None
        assert ticket.resolved_at is None
        assert ticket.closed_at is None


@pytest.mark.asyncio
async def test_concurrent_same_create_key_commits_one_ticket(
    clean_database: str, seeded_data: SeedData
) -> None:
    async with (
        _lifespan_client(clean_database) as first,
        _lifespan_client(clean_database) as second,
    ):
        csrf_a = await login(first, seeded_data.users.customer_a_email)
        csrf_b = await login(second, seeded_data.users.customer_a_email)
        payload = {
            "customer_email": seeded_data.users.customer_a_email,
            "category": "OTHER",
            "subject": "Retry race",
            "description": "Only one ticket should exist for this retry.",
            "priority": "LOW",
        }
        first_response, second_response = await asyncio.gather(
            first.post(
                "/api/v1/tickets", json=payload, headers=write_headers(csrf_a, "concurrent-create")
            ),
            second.post(
                "/api/v1/tickets", json=payload, headers=write_headers(csrf_b, "concurrent-create")
            ),
        )
    ids = {first_response.json()["id"], second_response.json()["id"]}
    async for session in session_for(clean_database):
        count = await session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.subject == "Retry race")
        )
    assert first_response.status_code == 201, first_response.text
    assert second_response.status_code == 201, second_response.text
    assert len(ids) == 1
    assert count == 1


@pytest.mark.asyncio
async def test_failed_create_rolls_back_side_effects(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    response = await seeded_app_client.post(
        "/api/v1/tickets",
        json={
            "customer_email": seed.users.customer_b_email,
            "customer_id": str(seed.users.customer_a_id),
            "category": "OTHER",
            "subject": "Mismatched customer",
            "description": "This must fail atomically.",
        },
        headers=write_headers(csrf, "rollback-create"),
    )

    assert response.status_code == 422
    async for session in session_for(clean_database):
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Ticket)
                .where(Ticket.subject == "Mismatched customer")
            )
        ) == 0
        assert (await session.scalar(select(func.count()).select_from(TicketEvent))) == 0
        assert (await session.scalar(select(func.count()).select_from(OutboxEvent))) == 0
        assert (await session.scalar(select(func.count()).select_from(IdempotencyRecord))) == 0


@pytest.mark.asyncio
async def test_first_clean_ticket_is_bd_1001_and_open(clean_database: str) -> None:
    """R03: numbers come from a sequence starting at 1001, not MAX(sequence) + 1."""
    async for session in session_for(clean_database):
        session.add(
            User(
                email="fresh.customer@example.test",
                display_name="Fresh Customer",
                password_hash=hash_password(TEST_PASSWORD),
                role=Role.CUSTOMER.value,
                is_active=True,
            )
        )
        await session.execute(text("SELECT setval('ticket_sequence_seq', 1000, true)"))
        await session.commit()

    async with _lifespan_client(clean_database) as client:
        csrf = await login(client, "fresh.customer@example.test")
        first = await client.post(
            "/api/v1/tickets",
            json={
                "customer_email": "fresh.customer@example.test",
                "category": "CONNECTIVITY",
                "subject": "First clean ticket",
                "description": "Proves the sequence, not a computed maximum.",
                "priority": "MEDIUM",
            },
            headers=write_headers(csrf, "fresh-first"),
        )
        second = await client.post(
            "/api/v1/tickets",
            json={
                "customer_email": "fresh.customer@example.test",
                "category": "OTHER",
                "subject": "Second clean ticket",
                "description": "The sequence continues without reuse.",
                "priority": "LOW",
            },
            headers=write_headers(csrf, "fresh-second"),
        )

    assert first.status_code == 201, first.text
    assert first.json()["ticket_number"] == "BD-1001"
    assert first.json()["status"] == "OPEN"
    assert first.json()["priority"] == "MEDIUM"
    assert second.status_code == 201, second.text
    assert second.json()["ticket_number"] == "BD-1002"


@pytest.mark.asyncio
async def test_concurrent_distinct_creates_allocate_unique_numbers(
    clean_database: str, seeded_data: SeedData
) -> None:
    """T02: concurrent independent creates never share a readable number."""
    async with (
        _lifespan_client(clean_database) as first,
        _lifespan_client(clean_database) as second,
    ):
        csrf_a = await login(first, seeded_data.users.customer_a_email)
        csrf_b = await login(second, seeded_data.users.customer_a_email)
        responses = await asyncio.gather(
            first.post(
                "/api/v1/tickets",
                json={
                    "customer_email": seeded_data.users.customer_a_email,
                    "category": "OTHER",
                    "subject": "Concurrent distinct A",
                    "description": "First of two independent creates.",
                    "priority": "LOW",
                },
                headers=write_headers(csrf_a, "distinct-create-a"),
            ),
            second.post(
                "/api/v1/tickets",
                json={
                    "customer_email": seeded_data.users.customer_a_email,
                    "category": "ORDER",
                    "subject": "Concurrent distinct B",
                    "description": "Second of two independent creates.",
                    "priority": "HIGH",
                },
                headers=write_headers(csrf_b, "distinct-create-b"),
            ),
        )

    for response in responses:
        assert response.status_code == 201, response.text
    ids = {response.json()["id"] for response in responses}
    numbers = {response.json()["ticket_number"] for response in responses}
    assert len(ids) == 2
    assert len(numbers) == 2
    assert all(number.startswith("BD-") for number in numbers)
    assert all(int(number.split("-")[1]) > 1002 for number in numbers)
    async for session in session_for(clean_database):
        created = await session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.subject.like("Concurrent distinct%"))
        )
    assert created == 2


@pytest.mark.asyncio
async def test_ticket_list_rejects_tampered_and_mismatched_cursor(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.agent_a_email)

    tampered = await seeded_app_client.get(
        "/api/v1/tickets", params={"limit": 1, "cursor": "not-a-cursor!!"}
    )
    first_page = await seeded_app_client.get("/api/v1/tickets", params={"limit": 1})
    cursor = first_page.json()["next_cursor"]
    mismatched = await seeded_app_client.get(
        "/api/v1/tickets", params={"limit": 1, "cursor": cursor, "status": "OPEN"}
    )

    assert first_page.status_code == 200, first_page.text
    assert cursor
    assert tampered.status_code == 422
    assert tampered.json()["error"]["code"] == "invalid_cursor"
    assert mismatched.status_code == 422
    assert mismatched.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_ticket_list_limit_bounds(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.agent_a_email)

    too_small = await seeded_app_client.get("/api/v1/tickets", params={"limit": 0})
    too_large = await seeded_app_client.get("/api/v1/tickets", params={"limit": 101})
    maximum = await seeded_app_client.get("/api/v1/tickets", params={"limit": 100})

    assert too_small.status_code == 422
    assert too_large.status_code == 422
    assert maximum.status_code == 200


@pytest.mark.asyncio
async def test_patch_rejects_unknown_field_and_empty_patch(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.agent_a_email)
    detail = await seeded_app_client.get(f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}")
    etag = detail.headers["etag"]

    unknown = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={"subject": "attempted rename"},
        headers=write_headers(csrf, etag=etag),
    )
    empty = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={},
        headers=write_headers(csrf, etag=etag),
    )

    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "validation_error"
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "empty_patch"


@pytest.mark.asyncio
async def test_large_thread_orders_and_pages_by_position(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.agent_a_email)
    ticket_id = seed.tickets.customer_a_ticket_id

    total = 45
    for index in range(total):
        response = await seeded_app_client.post(
            f"/api/v1/tickets/{ticket_id}/messages",
            json={"body": f"Message {index + 1}", "visibility": "PUBLIC"},
            headers=write_headers(csrf, f"large-thread-{index + 1}"),
        )
        assert response.status_code == 201, response.text

    newest = await seeded_app_client.get(
        f"/api/v1/tickets/{ticket_id}/messages", params={"limit": 20}
    )
    assert newest.status_code == 200, newest.text
    assert [item["position"] for item in newest.json()["items"]] == list(range(26, 46))
    assert newest.json()["has_more"] is True

    older = await seeded_app_client.get(
        f"/api/v1/tickets/{ticket_id}/messages",
        params={"limit": 20, "before": newest.json()["older_cursor"]},
    )
    assert [item["position"] for item in older.json()["items"]] == list(range(6, 26))

    oldest = await seeded_app_client.get(
        f"/api/v1/tickets/{ticket_id}/messages",
        params={"limit": 20, "before": older.json()["older_cursor"]},
    )
    assert [item["position"] for item in oldest.json()["items"]] == list(range(1, 6))
    assert oldest.json()["has_more"] is False
