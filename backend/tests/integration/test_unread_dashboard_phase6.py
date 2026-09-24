"""Private unread markers and the operational dashboard."""

from __future__ import annotations

from uuid import UUID

import pytest
from conftest import TEST_ORIGIN, SeedData, _lifespan_client
from httpx import AsyncClient, Response
from test_messages_phase3 import login, post_message

pytestmark = pytest.mark.asyncio


def csrf_headers(csrf: str) -> dict[str, str]:
    return {"Origin": TEST_ORIGIN, "X-CSRF-Token": csrf}


async def unread_for(client: AsyncClient, ticket_id: UUID) -> int:
    detail = await client.get(f"/api/v1/tickets/{ticket_id}")
    assert detail.status_code == 200, detail.text
    return int(detail.json()["unread_count"])


async def mark_seen(client: AsyncClient, ticket_id: UUID, csrf: str) -> Response:
    return await client.patch(f"/api/v1/tickets/{ticket_id}/seen", headers=csrf_headers(csrf))


async def test_customer_unread_counts_public_replies_and_excludes_internal_notes(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    agent_csrf = await login(seeded_app_client, seed.users.agent_a_email)

    async with _lifespan_client(clean_database) as customer:
        customer_csrf = await login(customer, seed.users.customer_a_email)
        assert await unread_for(customer, ticket_id) == 0

        await post_message(
            seeded_app_client, ticket_id, agent_csrf, body="Public reply", key="u-public"
        )
        assert await unread_for(customer, ticket_id) == 1

        await post_message(
            seeded_app_client,
            ticket_id,
            agent_csrf,
            body="Internal only",
            visibility="INTERNAL",
            key="u-internal",
        )
        # Internal notes never count as customer unread activity.
        assert await unread_for(customer, ticket_id) == 1

        seen = await mark_seen(customer, ticket_id, customer_csrf)
        assert seen.status_code == 204
        assert await unread_for(customer, ticket_id) == 0

        # The customer's own reply does not make their ticket unread.
        await post_message(customer, ticket_id, customer_csrf, body="My own reply", key="u-own")
        assert await unread_for(customer, ticket_id) == 0


async def test_staff_unread_counts_customer_activity_privately(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id

    async with _lifespan_client(clean_database) as customer:
        customer_csrf = await login(customer, seed.users.customer_a_email)
        await post_message(customer, ticket_id, customer_csrf, body="Customer asks", key="s-ask")

    async with _lifespan_client(clean_database) as other_agent:
        other_csrf = await login(other_agent, seed.users.agent_b_email)
        assert await unread_for(other_agent, ticket_id) == 1
        await mark_seen(other_agent, ticket_id, other_csrf)
        assert await unread_for(other_agent, ticket_id) == 0


async def test_seen_requires_csrf(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.customer_a_email)
    denied = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}/seen"
    )
    assert denied.status_code == 403


async def test_dashboard_summary_is_staff_only(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]

    async with _lifespan_client(clean_database) as customer:
        await login(customer, seed.users.customer_a_email)
        denied = await customer.get("/api/v1/dashboard/summary")
    assert denied.status_code == 403

    await login(seeded_app_client, seed.users.agent_a_email)
    response = await seeded_app_client.get("/api/v1/dashboard/summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_tickets"] == 2
    assert body["open_tickets"] == 2
    assert body["unassigned"] == 0
    assert body["overdue"] == 0
    assert body["policy_version"]
    assert {group["key"] for group in body["by_status"]} == {"OPEN"}
    assert set(body["first_response"]) == {"met", "breached", "pending"}
    assert set(body["resolution"]) == {"met", "breached", "pending"}
