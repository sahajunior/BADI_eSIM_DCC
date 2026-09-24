"""Read-only mock order context: ownership, unknown references, and safe failure."""

from __future__ import annotations

import pytest
from conftest import TEST_ORIGIN, SeedData, session_for
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from test_messages_phase3 import login

import app.orders as orders_module
from app.models import IdempotencyRecord, Ticket
from app.orders import MOCK_ORDERS, MockOrder

pytestmark = pytest.mark.asyncio


async def post_ticket(
    client: AsyncClient,
    csrf: str,
    email: str,
    *,
    subject: str,
    key: str,
    order_id: str | None = None,
) -> Response:
    payload: dict[str, object] = {
        "customer_email": email,
        "category": "CONNECTIVITY",
        "subject": subject,
        "description": "Order reference test.",
        "priority": "MEDIUM",
    }
    if order_id is not None:
        payload["order_id"] = order_id
    return await client.post(
        "/api/v1/tickets",
        json=payload,
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": key},
    )


async def test_customer_can_read_their_own_order(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    MOCK_ORDERS["ORD-OWNED-A"] = MockOrder(
        "ORD-OWNED-A", seed.users.customer_a_email, "Turkey", "10 GB", "completed", "installed"
    )
    try:
        await login(seeded_app_client, seed.users.customer_a_email)
        response = await seeded_app_client.get("/api/v1/mock/orders/ORD-OWNED-A")
    finally:
        MOCK_ORDERS.pop("ORD-OWNED-A", None)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "order_id": "ORD-OWNED-A",
        "destination": "Turkey",
        "package": "10 GB",
        "status": "completed",
        "esim_status": "installed",
    }


async def test_customer_cannot_read_another_customers_order(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    MOCK_ORDERS["ORD-OTHER-B"] = MockOrder(
        "ORD-OTHER-B", seed.users.customer_b_email, "Japan", "5 GB", "completed", "installed"
    )
    try:
        await login(seeded_app_client, seed.users.customer_a_email)
        response = await seeded_app_client.get("/api/v1/mock/orders/ORD-OTHER-B")
    finally:
        MOCK_ORDERS.pop("ORD-OTHER-B", None)

    # Other customers' orders are indistinguishable from unknown orders.
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "order_not_found"


async def test_staff_can_read_a_known_order_and_unknown_is_not_fabricated(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    MOCK_ORDERS["ORD-STAFF-1"] = MockOrder(
        "ORD-STAFF-1", seed.users.customer_a_email, "France", "5 GB", "completed", "installed"
    )
    try:
        await login(seeded_app_client, seed.users.agent_a_email)
        known = await seeded_app_client.get("/api/v1/mock/orders/ORD-STAFF-1")
        unknown = await seeded_app_client.get("/api/v1/mock/orders/ORD-DOES-NOT-EXIST")
    finally:
        MOCK_ORDERS.pop("ORD-STAFF-1", None)

    assert known.status_code == 200, known.text
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "order_not_found"


async def test_order_lookup_failure_is_a_safe_503(
    seeded_app_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.customer_a_email)

    def explode(_order_id: str) -> MockOrder | None:
        raise RuntimeError("provider credentials must not leak")

    monkeypatch.setattr(orders_module, "lookup_order", explode)
    response = await seeded_app_client.get("/api/v1/mock/orders/ORD-ANY")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "order_lookup_unavailable"
    assert "provider" not in response.text


async def test_ticket_creation_rejects_another_customers_known_order(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    MOCK_ORDERS["ORD-CONFLICT"] = MockOrder(
        "ORD-CONFLICT", seed.users.customer_b_email, "Japan", "5 GB", "completed", "installed"
    )
    try:
        csrf = await login(seeded_app_client, seed.users.customer_a_email)
        rejected = await post_ticket(
            seeded_app_client,
            csrf,
            seed.users.customer_a_email,
            subject="Order belonging to someone else",
            key="order-conflict",
            order_id="ORD-CONFLICT",
        )
    finally:
        MOCK_ORDERS.pop("ORD-CONFLICT", None)

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_order"
    async for session in session_for(clean_database):
        remaining = await session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.subject == "Order belonging to someone else")
        )
        idem = await session.scalar(select(func.count()).select_from(IdempotencyRecord))
    assert remaining == 0
    assert idem == 0


async def test_unknown_order_reference_is_allowed_as_a_plain_reference(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.customer_a_email)
    created = await post_ticket(
        seeded_app_client,
        csrf,
        seed.users.customer_a_email,
        subject="Plain unknown order reference",
        key="order-unknown",
        order_id="ORD-NOT-IN-FIXTURE",
    )
    assert created.status_code == 201, created.text
    assert created.json()["order_id"] == "ORD-NOT-IN-FIXTURE"
    listed = await seeded_app_client.get("/api/v1/mock/orders/ORD-NOT-IN-FIXTURE")
    assert listed.status_code == 404
