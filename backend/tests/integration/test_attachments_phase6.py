"""Attachments: validation, quarantine, message binding, and download authorization."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from conftest import TEST_ORIGIN, TEST_PASSWORD, SeedData, _lifespan_client, session_for
from httpx import AsyncClient, Response
from sqlalchemy import select

import app.attachments as attachments_module
from app.attachments import delete_expired
from app.models import Attachment
from app.scanning import ScannerUnavailable
from app.storage import storage_for
from app.unread import utcnow

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"


async def login(client: AsyncClient, email: str) -> str:
    csrf = await client.get("/api/v1/auth/csrf")
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": TEST_PASSWORD},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": csrf.json()["csrf_token"]},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


def write_headers(csrf: str, key: str = "attach-key") -> dict[str, str]:
    return {"Origin": TEST_ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": key}


async def upload(
    client: AsyncClient, ticket_id: UUID, csrf: str, name: str, content: bytes, ctype: str
) -> Response:
    return await client.post(
        f"/api/v1/tickets/{ticket_id}/attachments",
        files={"file": (name, content, ctype)},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": csrf},
    )


async def post_with_attachments(
    client: AsyncClient, ticket_id: UUID, csrf: str, attachment_ids: list[str], key: str
) -> Response:
    return await client.post(
        f"/api/v1/tickets/{ticket_id}/messages",
        json={"body": "See attached.", "visibility": "PUBLIC", "attachment_ids": attachment_ids},
        headers=write_headers(csrf, key),
    )


async def test_png_upload_attaches_and_downloads(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    uploaded = await upload(seeded_app_client, ticket_id, csrf, "diagram.png", PNG, "image/png")
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["scan_state"] == "CLEAN"
    attachment_id = uploaded.json()["id"]

    created = await post_with_attachments(
        seeded_app_client, ticket_id, csrf, [attachment_id], "attach-message"
    )
    assert created.status_code == 201, created.text
    assert created.json()["attachments"][0]["id"] == attachment_id

    listed = await seeded_app_client.get(f"/api/v1/tickets/{ticket_id}/messages")
    assert listed.json()["items"][0]["attachments"][0]["original_name"] == "diagram.png"

    downloaded = await seeded_app_client.get(f"/api/v1/attachments/{attachment_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.content == PNG
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.headers["x-content-type-options"] == "nosniff"


async def test_oversized_and_spoofed_and_unsupported_are_rejected(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    oversized = await upload(
        seeded_app_client,
        ticket_id,
        csrf,
        "big.png",
        PNG + b"\x00" * (10 * 1024 * 1024),
        "image/png",
    )
    html_spoof = await upload(
        seeded_app_client, ticket_id, csrf, "x.png", b"<html><script>alert(1)</script>", "image/png"
    )
    exe_spoof = await upload(
        seeded_app_client, ticket_id, csrf, "x.png", b"MZ\x90\x00binary", "image/png"
    )
    wrong_ext = await upload(seeded_app_client, ticket_id, csrf, "x.txt", PNG, "image/png")
    svg = await upload(
        seeded_app_client,
        ticket_id,
        csrf,
        "x.svg",
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        "image/svg+xml",
    )

    assert oversized.status_code == 413
    assert html_spoof.status_code == 422
    assert exe_spoof.status_code == 422
    assert wrong_ext.status_code == 422
    assert svg.status_code == 422


async def test_customer_cannot_download_another_ticket_attachment(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    agent_csrf = await login(seeded_app_client, seed.users.agent_a_email)
    uploaded = await upload(
        seeded_app_client, ticket_id, agent_csrf, "note.pdf", PDF, "application/pdf"
    )
    attachment_id = uploaded.json()["id"]
    assert uploaded.status_code == 201
    created = await post_with_attachments(
        seeded_app_client, ticket_id, agent_csrf, [attachment_id], "cross-user"
    )
    assert created.status_code == 201

    async with _lifespan_client(clean_database) as customer_b:
        await login(customer_b, seed.users.customer_b_email)
        denied = await customer_b.get(f"/api/v1/attachments/{attachment_id}/download")
    assert denied.status_code == 404


async def test_internal_message_attachment_is_never_customer_visible(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    agent_csrf = await login(seeded_app_client, seed.users.agent_a_email)
    uploaded = await upload(
        seeded_app_client, ticket_id, agent_csrf, "provider.pdf", PDF, "application/pdf"
    )
    attachment_id = uploaded.json()["id"]
    created = await seeded_app_client.post(
        f"/api/v1/tickets/{ticket_id}/messages",
        json={
            "body": "Internal evidence.",
            "visibility": "INTERNAL",
            "attachment_ids": [attachment_id],
        },
        headers=write_headers(agent_csrf, "internal-attach"),
    )
    assert created.status_code == 201, created.text

    async with _lifespan_client(clean_database) as customer:
        await login(customer, seed.users.customer_a_email)
        denied = await customer.get(f"/api/v1/attachments/{attachment_id}/download")
        messages = await customer.get(f"/api/v1/tickets/{ticket_id}/messages")
    assert denied.status_code == 404
    assert messages.status_code == 200
    assert all(not item["attachments"] for item in messages.json()["items"])


async def test_attachment_cross_ticket_and_double_bind_are_rejected(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_a = seed.tickets.customer_a_ticket_id
    ticket_b = seed.tickets.customer_b_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    uploaded = await upload(seeded_app_client, ticket_a, csrf, "a.png", PNG, "image/png")
    attachment_id = uploaded.json()["id"]

    cross = await post_with_attachments(
        seeded_app_client, ticket_b, csrf, [attachment_id], "cross-ticket"
    )
    assert cross.status_code == 422
    assert cross.json()["error"]["code"] == "invalid_attachment"

    first = await post_with_attachments(
        seeded_app_client, ticket_a, csrf, [attachment_id], "bind-once"
    )
    assert first.status_code == 201, first.text
    second = await post_with_attachments(
        seeded_app_client, ticket_a, csrf, [attachment_id], "bind-twice"
    )
    assert second.status_code == 422


async def test_staged_attachment_is_uploader_only_until_attached(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)
    uploaded = await upload(seeded_app_client, ticket_id, csrf, "staged.png", PNG, "image/png")
    attachment_id = uploaded.json()["id"]

    own = await seeded_app_client.get(f"/api/v1/attachments/{attachment_id}/download")
    assert own.status_code == 200

    async with _lifespan_client(clean_database) as other_agent:
        await login(other_agent, seed.users.agent_b_email)
        denied = await other_agent.get(f"/api/v1/attachments/{attachment_id}/download")
    assert denied.status_code == 404


async def test_scanning_disabled_fails_closed(
    seeded_app_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    def unavailable(_data: bytes, _name: str, _settings: object) -> str:
        raise ScannerUnavailable("disabled")

    monkeypatch.setattr(attachments_module, "scan", unavailable)
    response = await upload(seeded_app_client, ticket_id, csrf, "x.png", PNG, "image/png")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "scanning_unavailable"


async def test_cleanup_removes_only_expired_unattached(
    seeded_app_client: AsyncClient, clean_database: str
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    ticket_id = seed.tickets.customer_a_ticket_id
    csrf = await login(seeded_app_client, seed.users.agent_a_email)

    staged = await upload(seeded_app_client, ticket_id, csrf, "old.png", PNG, "image/png")
    staged_id = UUID(staged.json()["id"])
    bound = await upload(seeded_app_client, ticket_id, csrf, "keep.png", PNG, "image/png")
    bound_id = bound.json()["id"]
    assert (
        await post_with_attachments(seeded_app_client, ticket_id, csrf, [bound_id], "cleanup")
    ).status_code == 201

    async for session in session_for(clean_database):
        from app.config import Settings

        settings = Settings(
            _env_file=None,
            auth_secret="x" * 40,
            database_url=clean_database,
            environment="test",
            attachment_dir="/tmp/badi-cleanup-not-used",
        )
        store = storage_for(settings)
        removed = await delete_expired(session, store, utcnow() + timedelta(days=2))
        await session.commit()
        remaining = set(await session.scalars(select(Attachment.id)))

    assert removed == 1
    assert staged_id not in remaining
    assert UUID(bound_id) in remaining
