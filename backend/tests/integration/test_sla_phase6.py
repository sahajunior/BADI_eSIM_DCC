"""SLA endpoint, status/priority transitions, and worker breach recording."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import TEST_ORIGIN, SeedData, _lifespan_client, session_for
from httpx import AsyncClient, Response
from sqlalchemy import func, select, update
from test_messages_phase3 import login, post_message

from app.models import OutboxEvent, TicketEvent, TicketSlaCycle
from app.sla import record_breaches, resolution_target

pytestmark = pytest.mark.asyncio


def write_headers(csrf: str, etag: str | None = None) -> dict[str, str]:
    headers = {"Origin": TEST_ORIGIN, "X-CSRF-Token": csrf}
    if etag is not None:
        headers["If-Match"] = etag
    return headers


async def patch_status(
    client: AsyncClient, ticket_id: UUID, csrf: str, status: str, **extra: object
) -> Response:
    detail = await client.get(f"/api/v1/tickets/{ticket_id}")
    payload: dict[str, object] = {"status": status}
    payload.update(extra)
    return await client.patch(
        f"/api/v1/tickets/{ticket_id}",
        json=payload,
        headers=write_headers(csrf, etag=detail.headers["etag"]),
    )


async def test_sla_endpoint_is_staff_only_and_projects_state(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id

    async with _lifespan_client(clean_database) as customer:
        await login(customer, seed.users.customer_a_email)
        denied = await customer.get(f"/api/v1/tickets/{ticket_id}/sla")
    assert denied.status_code == 403

    await login(seeded_app_client, seed.users.agent_a_email)
    response = await seeded_app_client.get(f"/api/v1/tickets/{ticket_id}/sla")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cycle_number"] == 1
    assert body["priority"] == "HIGH"
    assert body["first_response"]["state"] in {"on_track", "due_soon", "overdue"}
    assert body["first_response"]["target_seconds"] == 60 * 60
    assert body["resolution"]["target_seconds"] == resolution_target("HIGH")


async def test_first_public_reply_marks_first_response_met(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    reply = await post_message(
        seeded_app_client, ticket_id, csrf, body="Looking into this.", key="fr-reply"
    )
    assert reply.status_code == 201, reply.text

    sla = await seeded_app_client.get(f"/api/v1/tickets/{ticket_id}/sla")
    assert sla.json()["first_response"]["state"] == "met"
    assert sla.json()["first_response"]["met_at"] is not None


async def test_waiting_for_customer_pauses_and_resumes_resolution(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    waiting = await patch_status(seeded_app_client, ticket_id, csrf, "WAITING_FOR_CUSTOMER")
    assert waiting.status_code == 200, waiting.text
    paused = await seeded_app_client.get(f"/api/v1/tickets/{ticket_id}/sla")
    assert paused.json()["resolution"]["state"] == "paused"

    resumed = await patch_status(seeded_app_client, ticket_id, csrf, "IN_PROGRESS")
    assert resumed.status_code == 200, resumed.text
    active = await seeded_app_client.get(f"/api/v1/tickets/{ticket_id}/sla")
    assert active.json()["resolution"]["state"] != "paused"


async def test_priority_change_recalculates_without_resetting_cycle(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    before = (await seeded_app_client.get(f"/api/v1/tickets/{ticket_id}/sla")).json()
    changed = await patch_status(seeded_app_client, ticket_id, csrf, "OPEN", priority="URGENT")
    assert changed.status_code == 200, changed.text
    after = (await seeded_app_client.get(f"/api/v1/tickets/{ticket_id}/sla")).json()

    assert after["cycle_number"] == before["cycle_number"] == 1
    assert after["priority"] == "URGENT"
    assert after["resolution"]["target_seconds"] == resolution_target("URGENT")


async def test_resolve_ends_cycle_and_reopen_starts_new_cycle(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    await post_message(
        seeded_app_client, ticket_id, csrf, body="Resolved explanation.", key="resolve-reply"
    )
    resolved = await patch_status(seeded_app_client, ticket_id, csrf, "RESOLVED")
    assert resolved.status_code == 200, resolved.text
    ended = await seeded_app_client.get(f"/api/v1/tickets/{ticket_id}/sla")
    assert ended.status_code == 404
    assert ended.json()["error"]["code"] == "sla_unavailable"

    reopened = await patch_status(
        seeded_app_client, ticket_id, csrf, "IN_PROGRESS", reason="Customer came back."
    )
    assert reopened.status_code == 200, reopened.text
    fresh = await seeded_app_client.get(f"/api/v1/tickets/{ticket_id}/sla")
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["cycle_number"] == 2
    # The historical first-response result is carried forward as already met.
    assert fresh.json()["first_response"]["state"] == "met"


async def test_worker_records_breaches_once_and_emits_staff_events(
    seeded_data: SeedData, clean_database: str
) -> None:
    ticket_id = seeded_data.tickets.customer_a_ticket_id
    past = datetime.now(UTC) - timedelta(hours=1)

    async for session in session_for(clean_database):
        await session.execute(
            update(TicketSlaCycle)
            .where(TicketSlaCycle.ticket_id == ticket_id)
            .values(
                first_response_due_at=past,
                resolution_due_at=past,
                resolution_pause_started_at=None,
            )
        )
        await session.commit()

    async for session in session_for(clean_database):
        first_run = await record_breaches(session, datetime.now(UTC))
        await session.commit()
        second_run = await record_breaches(session, datetime.now(UTC))
        await session.commit()
        events = await session.scalar(
            select(func.count())
            .select_from(TicketEvent)
            .where(
                TicketEvent.ticket_id == ticket_id,
                TicketEvent.event_type == "sla.breached",
            )
        )
        fields = set(
            await session.scalars(
                select(TicketEvent.field).where(
                    TicketEvent.ticket_id == ticket_id,
                    TicketEvent.event_type == "sla.breached",
                )
            )
        )
        internal_outbox = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.ticket_id == ticket_id,
                OutboxEvent.visibility == "INTERNAL",
            )
        )

    assert len(first_run) == 2
    assert second_run == []
    assert events == 2
    assert fields == {"first_response", "resolution"}
    assert internal_outbox and internal_outbox >= 2
