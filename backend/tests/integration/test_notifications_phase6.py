"""Reply notification matrix, retries, preferences, and content safety."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from conftest import TEST_ORIGIN, SeedData, _lifespan_client, session_for
from httpx import AsyncClient
from sqlalchemy import select, update
from test_messages_phase3 import login, post_message

import app.notifications as notifications_module
from app.config import Settings
from app.models import (
    NotificationDelivery,
    NotificationPreference,
    Ticket,
)
from app.notifications import process_deliveries, render_email

pytestmark = pytest.mark.asyncio


def settings_for(_url: str) -> Settings:
    return Settings(
        _env_file=None,
        auth_secret="x" * 40,
        database_url="postgresql+asyncpg://badi:badi-tests-only@127.0.0.1:55433/badi_test",
        environment="test",
        email_backend="console",
    )


async def deliveries(clean_database: str, ticket_id: UUID) -> list[NotificationDelivery]:
    async for session in session_for(clean_database):
        return list(
            await session.scalars(
                select(NotificationDelivery)
                .where(NotificationDelivery.ticket_id == ticket_id)
                .order_by(NotificationDelivery.channel, NotificationDelivery.id)
            )
        )
    raise AssertionError("session unavailable")


async def process(clean_database: str, settings: Settings, now: datetime) -> list[str]:
    async for session in session_for(clean_database):
        outcomes = await process_deliveries(session, settings, now)
        await session.commit()
        return outcomes
    raise AssertionError("session unavailable")


async def test_staff_public_reply_emails_the_customer_only(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)
    await post_message(seeded_app_client, ticket_id, csrf, body="Public answer", key="n-staff")

    rows = await deliveries(clean_database, ticket_id)
    assert len(rows) == 1
    assert rows[0].channel == "email"
    assert rows[0].recipient_id == seed.users.customer_a_id
    assert rows[0].status == "PENDING"

    outcomes = await process(clean_database, settings_for(clean_database), datetime.now(UTC))
    assert outcomes == ["sent"]
    assert (await deliveries(clean_database, ticket_id))[0].status == "SENT"


async def test_customer_reply_emails_only_the_assigned_agent(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.customer_a_email)
    await post_message(seeded_app_client, ticket_id, csrf, body="Customer update", key="n-cust")

    rows = await deliveries(clean_database, ticket_id)
    assert len(rows) == 1
    assert rows[0].recipient_id == seed.users.agent_a_id


async def test_internal_notes_never_enqueue_notifications(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)
    await post_message(
        seeded_app_client,
        ticket_id,
        csrf,
        body="Internal only",
        visibility="INTERNAL",
        key="n-internal",
    )
    assert await deliveries(clean_database, ticket_id) == []


async def test_unassigned_customer_reply_queues_without_email(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    async for session in session_for(clean_database):
        await session.execute(
            update(Ticket).where(Ticket.id == ticket_id).values(assigned_agent_id=None)
        )
        await session.commit()

    csrf = await login(seeded_app_client, seed.users.customer_a_email)
    await post_message(seeded_app_client, ticket_id, csrf, body="Anyone there?", key="n-unassigned")

    rows = await deliveries(clean_database, ticket_id)
    assert len(rows) == 1
    assert rows[0].channel == "queue"
    assert rows[0].recipient_id is None
    assert await process(clean_database, settings_for(clean_database), datetime.now(UTC)) == [
        "queue"
    ]


async def test_author_is_never_notified(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.customer_a_email)
    await post_message(seeded_app_client, ticket_id, csrf, body="Mine", key="n-author")
    rows = await deliveries(clean_database, ticket_id)
    assert all(row.recipient_id != seed.users.customer_a_id for row in rows)


async def test_preference_disabled_skips_delivery(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    async for session in session_for(clean_database):
        session.add(NotificationPreference(user_id=seed.users.customer_a_id, email_on_reply=False))
        await session.commit()

    csrf = await login(seeded_app_client, seed.users.agent_a_email)
    await post_message(seeded_app_client, ticket_id, csrf, body="Answer", key="n-pref")
    outcomes = await process(clean_database, settings_for(clean_database), datetime.now(UTC))
    assert outcomes == ["skipped"]
    assert (await deliveries(clean_database, ticket_id))[0].status == "SKIPPED"


async def test_provider_outage_retries_then_dead_letters(
    seeded_app_client: AsyncClient, clean_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)
    await post_message(seeded_app_client, ticket_id, csrf, body="Answer", key="n-outage")

    def boom(_settings: Settings, _to: str, _subject: str, _body: str) -> str:
        raise RuntimeError("provider down")

    monkeypatch.setattr(notifications_module, "_send_email", boom)
    settings = settings_for(clean_database)
    settings.notification_max_attempts = 2

    first = await process(clean_database, settings, datetime.now(UTC))
    assert first == ["retry"]
    row = (await deliveries(clean_database, ticket_id))[0]
    assert row.status == "PENDING" and row.attempts == 1 and row.last_error == "send_failed"

    async for session in session_for(clean_database):
        await session.execute(update(NotificationDelivery).values(available_at=datetime.now(UTC)))
        await session.commit()
    second = await process(clean_database, settings, datetime.now(UTC))
    assert second == ["failed"]
    assert (await deliveries(clean_database, ticket_id))[0].status == "FAILED"


async def test_retry_endpoint_requeues_a_failed_delivery(
    seeded_app_client: AsyncClient, clean_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    agent_csrf = await login(seeded_app_client, seed.users.agent_a_email)
    await post_message(seeded_app_client, ticket_id, agent_csrf, body="Answer", key="n-retry")

    def boom(_settings: Settings, _to: str, _subject: str, _body: str) -> str:
        raise RuntimeError("provider down")

    monkeypatch.setattr(notifications_module, "_send_email", boom)
    settings = settings_for(clean_database)
    settings.notification_max_attempts = 1
    assert await process(clean_database, settings, datetime.now(UTC)) == ["failed"]

    delivery_id = (await deliveries(clean_database, ticket_id))[0].id
    failed = await seeded_app_client.get("/api/v1/notifications/failed")
    assert failed.status_code == 200
    assert any(item["id"] == str(delivery_id) for item in failed.json()["items"])

    retried = await seeded_app_client.post(
        f"/api/v1/notifications/{delivery_id}/retry",
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": agent_csrf},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "PENDING"


async def test_preference_endpoint_persists_and_requires_csrf(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)

    initial = await seeded_app_client.get("/api/v1/notification-preferences")
    assert initial.status_code == 200
    assert initial.json()["email_on_reply"] is True

    missing_csrf = await seeded_app_client.patch(
        "/api/v1/notification-preferences", json={"email_on_reply": False}
    )
    assert missing_csrf.status_code == 403

    updated = await seeded_app_client.patch(
        "/api/v1/notification-preferences",
        json={"email_on_reply": False},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["email_on_reply"] is False

    # A separate client sees the persisted value.
    async with _lifespan_client(clean_database) as other:
        await login(other, seed.users.customer_a_email)
        reread = await other.get("/api/v1/notification-preferences")
    assert reread.json()["email_on_reply"] is False


async def test_rendered_email_omits_body_attachments_and_internal_content(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)
    secret = "PATIENT-PROVIDER-SECRET-7788"
    await post_message(seeded_app_client, ticket_id, csrf, body=secret, key="n-content")

    async for session in session_for(clean_database):
        ticket = await session.get(Ticket, ticket_id)
        assert ticket is not None
        subject, body = render_email(ticket, settings_for(clean_database))
        number = ticket.ticket_number
    assert number in subject
    assert number in body
    assert "/login" in body
    assert secret not in body


async def test_replayed_message_does_not_duplicate_delivery(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)
    payload_headers = {"Origin": TEST_ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": "n-replay"}

    first = await seeded_app_client.post(
        f"/api/v1/tickets/{ticket_id}/messages",
        json={"body": "Once", "visibility": "PUBLIC"},
        headers=payload_headers,
    )
    second = await seeded_app_client.post(
        f"/api/v1/tickets/{ticket_id}/messages",
        json={"body": "Once", "visibility": "PUBLIC"},
        headers=payload_headers,
    )
    assert first.status_code == second.status_code == 201
    assert len(await deliveries(clean_database, ticket_id)) == 1
