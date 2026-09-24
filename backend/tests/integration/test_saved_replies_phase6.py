"""Saved replies: staff-only management, author/admin mutation, validation."""

from __future__ import annotations

import pytest
from conftest import TEST_ORIGIN, SeedData, _lifespan_client
from httpx import AsyncClient, Response
from test_messages_phase3 import login

pytestmark = pytest.mark.asyncio


def headers(csrf: str) -> dict[str, str]:
    return {"Origin": TEST_ORIGIN, "X-CSRF-Token": csrf}


async def create(client: AsyncClient, csrf: str, title: str, body: str) -> Response:
    return await client.post(
        "/api/v1/saved-replies", json={"title": title, "body": body}, headers=headers(csrf)
    )


async def test_staff_can_create_and_list_saved_replies(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    empty = await seeded_app_client.get("/api/v1/saved-replies")
    assert empty.status_code == 200
    assert empty.json()["items"] == []

    created = await create(
        seeded_app_client, csrf, "Connectivity follow-up", "Please share your country."
    )
    assert created.status_code == 201, created.text
    assert created.json()["is_active"] is True
    assert created.json()["author_id"] == str(seed.users.agent_a_id)

    listed = await seeded_app_client.get("/api/v1/saved-replies")
    assert [item["id"] for item in listed.json()["items"]] == [created.json()["id"]]


async def test_customers_cannot_manage_saved_replies(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    async with _lifespan_client(clean_database) as customer:
        csrf = await login(customer, "customer.a@example.test")
        listed = await customer.get("/api/v1/saved-replies")
        created = await create(customer, csrf, "Nope", "Should not work")
        assert listed.status_code == 403
        assert created.status_code in {403, 401}


async def test_only_author_or_admin_can_update(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.agent_a_email)
    created = await create(seeded_app_client, csrf, "Shared macro", "Original body.")
    reply_id = created.json()["id"]

    async with _lifespan_client(clean_database) as other_agent:
        other_csrf = await login(other_agent, seed.users.agent_b_email)
        denied = await other_agent.patch(
            f"/api/v1/saved-replies/{reply_id}",
            json={"title": "Hijacked"},
            headers=headers(other_csrf),
        )
    assert denied.status_code == 403

    async with _lifespan_client(clean_database) as admin:
        admin_csrf = await login(admin, seed.users.admin_email)
        allowed = await admin.patch(
            f"/api/v1/saved-replies/{reply_id}",
            json={"body": "Edited by an administrator."},
            headers=headers(admin_csrf),
        )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["body"] == "Edited by an administrator."

    # The author can also update, including deactivating.
    patched = await seeded_app_client.patch(
        f"/api/v1/saved-replies/{reply_id}",
        json={"is_active": False},
        headers=headers(csrf),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["is_active"] is False

    active = await seeded_app_client.get("/api/v1/saved-replies")
    everything = await seeded_app_client.get("/api/v1/saved-replies", params={"active_only": False})
    assert active.json()["items"] == []
    assert [item["id"] for item in everything.json()["items"]] == [reply_id]


async def test_saved_reply_validation(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    blank = await create(seeded_app_client, csrf, "   ", "body")
    unknown = await seeded_app_client.post(
        "/api/v1/saved-replies",
        json={"title": "x", "body": "y", "author_id": "spoof"},
        headers=headers(csrf),
    )
    created = await create(seeded_app_client, csrf, "Valid macro", "Valid body.")
    empty_patch = await seeded_app_client.patch(
        f"/api/v1/saved-replies/{created.json()['id']}", json={}, headers=headers(csrf)
    )
    missing = await seeded_app_client.patch(
        "/api/v1/saved-replies/11111111-1111-1111-1111-111111111111",
        json={"title": "x"},
        headers=headers(csrf),
    )

    assert blank.status_code == 422
    assert unknown.status_code == 422
    assert empty_patch.status_code == 422
    assert empty_patch.json()["error"]["code"] == "empty_patch"
    assert missing.status_code == 404
