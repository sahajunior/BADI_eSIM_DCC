from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from conftest import TEST_ORIGIN, TEST_PASSWORD, SeedData, session_for
from httpx import AsyncClient
from sqlalchemy import select, text, update

CUSTOMER_KEYS = {
    "id",
    "ticket_number",
    "subject",
    "description",
    "category",
    "priority",
    "status",
    "order_id",
    "created_at",
    "public_updated_at",
    "unread_count",
}
STAFF_EXTRA_KEYS = {
    "customer_id",
    "customer_email_snapshot",
    "created_by_id",
    "assigned_agent_id",
    "version",
    "updated_at",
}
PRIVATE_CUSTOMER_KEYS = STAFF_EXTRA_KEYS


async def login(
    client: AsyncClient,
    email: str,
    password: str = TEST_PASSWORD,
    *,
    origin: str | None = TEST_ORIGIN,
) -> dict[str, Any]:
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    csrf_token = csrf.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf_token}
    if origin is not None:
        headers["Origin"] = origin
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return {"body": response.json(), "csrf_token": response.json()["csrf_token"]}


async def csrf_token(client: AsyncClient) -> str:
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    assert "badi_session" in client.cookies
    return cast(str, response.json()["csrf_token"])


async def auth_headers(client: AsyncClient) -> dict[str, str]:
    return {"Origin": TEST_ORIGIN, "X-CSRF-Token": await csrf_token(client)}


@pytest.mark.asyncio
async def test_csrf_bootstrap_sets_httponly_session_cookie(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/auth/csrf")

    assert response.status_code == 200
    assert response.json()["csrf_token"]
    assert "badi_session" in app_client.cookies
    assert "httponly" in response.headers["set-cookie"].lower()


@pytest.mark.asyncio
async def test_login_returns_sanitized_user_and_rotates_csrf(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    first_csrf = await csrf_token(seeded_app_client)

    response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": seed.users.customer_a_email, "password": TEST_PASSWORD},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": first_csrf},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == seed.users.customer_a_email
    assert body["display_name"] == "Customer Alpha"
    assert body["role"] == "CUSTOMER"
    assert body["csrf_token"] != first_csrf
    assert "password" not in body
    assert "password_hash" not in body


@pytest.mark.asyncio
async def test_login_rejects_extra_fields(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    token = await csrf_token(seeded_app_client)

    response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": seed.users.customer_a_email, "password": TEST_PASSWORD, "role": "ADMIN"},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": token},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_login_requires_csrf_header(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await csrf_token(seeded_app_client)

    response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": seed.users.customer_a_email, "password": TEST_PASSWORD},
        headers={"Origin": TEST_ORIGIN},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_login_rejects_csrf_mismatch(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await csrf_token(seeded_app_client)

    response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": seed.users.customer_a_email, "password": TEST_PASSWORD},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": "wrong-token"},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_login_rejects_foreign_origin(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    token = await csrf_token(seeded_app_client)

    response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": seed.users.customer_a_email, "password": TEST_PASSWORD},
        headers={"Origin": "https://evil.example", "X-CSRF-Token": token},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_login_allows_valid_referer_when_origin_missing(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    token = await csrf_token(seeded_app_client)

    response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": seed.users.customer_a_email, "password": TEST_PASSWORD},
        headers={"Referer": f"{TEST_ORIGIN}/login", "X-CSRF-Token": token},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_login_rejects_missing_origin_and_referer(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    token = await csrf_token(seeded_app_client)

    response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": seed.users.customer_a_email, "password": TEST_PASSWORD},
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("email", "password"),
    [("missing@example.test", TEST_PASSWORD), ("customer.a@example.test", "wrong-password")],
)
async def test_login_rejects_unknown_or_wrong_password_with_generic_401(
    seeded_app_client: AsyncClient,
    email: str,
    password: str,
) -> None:
    token = await csrf_token(seeded_app_client)

    response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": token},
    )

    assert response.status_code == 401
    assert "wrong" not in response.text.lower()
    assert "missing@example.test" not in response.text


@pytest.mark.asyncio
async def test_login_rejects_inactive_user(
    seeded_app_client: AsyncClient,
    clean_database: str,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    from app.models import User

    async for session in session_for(clean_database):
        await session.execute(
            update(User).where(User.email == seed.users.customer_a_email).values(is_active=False)
        )
        await session.commit()

    token = await csrf_token(seeded_app_client)
    response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": seed.users.customer_a_email, "password": TEST_PASSWORD},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": token},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me_requires_authentication(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/auth/me")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me_returns_sanitized_current_user(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.customer_a_email)

    response = await seeded_app_client.get("/api/v1/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == seed.users.customer_a_email
    assert set(body) == {"id", "email", "display_name", "role"}


@pytest.mark.asyncio
async def test_logout_requires_csrf(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.customer_a_email)

    response = await seeded_app_client.post("/api/v1/auth/logout", headers={"Origin": TEST_ORIGIN})

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_logout_revokes_session(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    auth = await login(seeded_app_client, seed.users.customer_a_email)

    logout_response = await seeded_app_client.post(
        "/api/v1/auth/logout",
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": auth["csrf_token"]},
    )
    me_response = await seeded_app_client.get("/api/v1/auth/me")

    assert logout_response.status_code == 204
    assert me_response.status_code == 401


@pytest.mark.asyncio
async def test_session_rotation_makes_bootstrap_token_unusable(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    bootstrap_token = await csrf_token(seeded_app_client)
    login_response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": seed.users.customer_a_email, "password": TEST_PASSWORD},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": bootstrap_token},
    )
    assert login_response.status_code == 200

    response = await seeded_app_client.post(
        "/api/v1/auth/logout",
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": bootstrap_token},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_database_stores_session_hash_only(
    seeded_app_client: AsyncClient,
    clean_database: str,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    auth = await login(seeded_app_client, seed.users.customer_a_email)
    from app.models import AuthSession

    async for session in session_for(clean_database):
        rows = (await session.execute(select(AuthSession))).scalars().all()

    assert rows
    assert all(row.token_hash for row in rows)
    assert all(auth["csrf_token"] not in row.token_hash for row in rows)


@pytest.mark.asyncio
async def test_database_never_stores_plaintext_password(
    seeded_app_client: AsyncClient,
    clean_database: str,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    from app.models import User

    async for session in session_for(clean_database):
        user = (
            await session.execute(select(User).where(User.email == seed.users.customer_a_email))
        ).scalar_one()

    assert user.password_hash != TEST_PASSWORD
    assert TEST_PASSWORD not in user.password_hash


@pytest.mark.asyncio
async def test_expired_session_is_rejected(
    seeded_app_client: AsyncClient,
    clean_database: str,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.customer_a_email)
    from app.models import AuthSession

    async for session in session_for(clean_database):
        await session.execute(
            update(AuthSession).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()

    response = await seeded_app_client.get("/api/v1/auth/me")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_revoked_session_is_rejected(
    seeded_app_client: AsyncClient,
    clean_database: str,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.customer_a_email)
    from app.models import AuthSession

    async for session in session_for(clean_database):
        await session.execute(update(AuthSession).values(revoked_at=datetime.now(UTC)))
        await session.commit()

    response = await seeded_app_client.get("/api/v1/auth/me")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_deactivated_user_current_session_is_rejected(
    seeded_app_client: AsyncClient,
    clean_database: str,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.customer_a_email)
    from app.models import User

    async for session in session_for(clean_database):
        await session.execute(
            update(User).where(User.email == seed.users.customer_a_email).values(is_active=False)
        )
        await session.commit()

    response = await seeded_app_client.get("/api/v1/auth/me")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_customer_can_read_own_ticket_with_public_shape(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.customer_a_email)

    response = await seeded_app_client.get(f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == CUSTOMER_KEYS
    assert body["subject"] == "Customer A eSIM activation issue"
    assert not (PRIVATE_CUSTOMER_KEYS & set(body))


@pytest.mark.asyncio
async def test_customer_gets_404_for_other_customers_ticket(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.customer_a_email)

    response = await seeded_app_client.get(f"/api/v1/tickets/{seed.tickets.customer_b_ticket_id}")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_customer_gets_404_for_nonexistent_ticket(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.customer_a_email)

    response = await seeded_app_client.get("/api/v1/tickets/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_agent_can_read_any_ticket_with_staff_shape(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.agent_a_email)

    first = await seeded_app_client.get(f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}")
    second = await seeded_app_client.get(f"/api/v1/tickets/{seed.tickets.customer_b_ticket_id}")

    assert first.status_code == 200
    assert second.status_code == 200
    assert set(first.json()) == CUSTOMER_KEYS | STAFF_EXTRA_KEYS
    assert set(second.json()) == CUSTOMER_KEYS | STAFF_EXTRA_KEYS


@pytest.mark.asyncio
async def test_admin_can_read_any_ticket_with_staff_shape(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.admin_email)

    response = await seeded_app_client.get(f"/api/v1/tickets/{seed.tickets.customer_b_ticket_id}")

    assert response.status_code == 200
    assert set(response.json()) == CUSTOMER_KEYS | STAFF_EXTRA_KEYS


@pytest.mark.asyncio
async def test_ticket_read_requires_authentication(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]

    response = await seeded_app_client.get(f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_customer_cannot_list_agents(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.customer_a_email)

    response = await seeded_app_client.get("/api/v1/agents")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_anonymous_cannot_list_agents(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/agents")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_staff_can_list_agents_without_private_emails(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.agent_a_email)

    response = await seeded_app_client.get("/api/v1/agents")

    assert response.status_code == 200
    body = response.json()
    assert {"items"} == set(body)
    assert {"id", "display_name"} == set(body["items"][0])
    assert all("email" not in item for item in body["items"])


@pytest.mark.asyncio
async def test_ticket_write_routes_require_their_current_methods_and_guards(
    seeded_app_client: AsyncClient,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    await login(seeded_app_client, seed.users.agent_a_email)

    wrong_method = await seeded_app_client.post(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={"status": "RESOLVED"},
        headers=await auth_headers(seeded_app_client),
    )
    missing_precondition = await seeded_app_client.patch(
        f"/api/v1/tickets/{seed.tickets.customer_a_ticket_id}",
        json={"status": "RESOLVED"},
        headers=await auth_headers(seeded_app_client),
    )

    assert wrong_method.status_code == 405
    assert missing_precondition.status_code == 428


@pytest.mark.asyncio
async def test_role_spoof_payload_does_not_escalate_user(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    token = await csrf_token(seeded_app_client)

    response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": seed.users.customer_a_email, "password": TEST_PASSWORD, "role": "ADMIN"},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": token},
    )
    agents = await seeded_app_client.get("/api/v1/agents")

    assert response.status_code == 422
    assert agents.status_code == 401


@pytest.mark.asyncio
async def test_login_rate_limit_counts_failures_per_email(seeded_app_client: AsyncClient) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    last_response = None
    for _ in range(6):
        token = await csrf_token(seeded_app_client)
        last_response = await seeded_app_client.post(
            "/api/v1/auth/login",
            json={"email": seed.users.customer_a_email, "password": "wrong-password"},
            headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": token},
        )

    assert last_response is not None
    assert last_response.status_code == 429


@pytest.mark.asyncio
async def test_login_rate_limit_counts_failures_per_ip(seeded_app_client: AsyncClient) -> None:
    last_response = None
    for index in range(21):
        token = await csrf_token(seeded_app_client)
        last_response = await seeded_app_client.post(
            "/api/v1/auth/login",
            json={"email": f"missing-{index}@example.test", "password": "wrong-password"},
            headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": token},
        )

    assert last_response is not None
    assert last_response.status_code == 429


@pytest.mark.asyncio
async def test_email_throttle_uses_hashed_key_and_resets_after_window(
    seeded_app_client: AsyncClient,
    clean_database: str,
) -> None:
    seed: SeedData = seeded_app_client.seeded_data  # type: ignore[attr-defined]
    from app.auth.security import THROTTLE_WINDOW_SECONDS, throttle_key_hash

    for _ in range(2):
        token = await csrf_token(seeded_app_client)
        response = await seeded_app_client.post(
            "/api/v1/auth/login",
            json={"email": seed.users.customer_a_email, "password": "wrong-password"},
            headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": token},
        )
        assert response.status_code == 401

    email_key_hash = throttle_key_hash("auth:login:email", seed.users.customer_a_email)
    async for session in session_for(clean_database):
        attempts = await session.scalar(
            text("SELECT attempts FROM auth_throttles WHERE key_hash = :key_hash"),
            {"key_hash": email_key_hash},
        )

    assert attempts == 2
    assert seed.users.customer_a_email not in email_key_hash

    async for session in session_for(clean_database):
        await session.execute(
            text(
                "UPDATE auth_throttles "
                "SET window_started_at = :stale_started_at "
                "WHERE key_hash = :key_hash"
            ),
            {
                "stale_started_at": datetime.now(UTC)
                - timedelta(seconds=THROTTLE_WINDOW_SECONDS + 1),
                "key_hash": email_key_hash,
            },
        )
        await session.commit()

    token = await csrf_token(seeded_app_client)
    response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": seed.users.customer_a_email, "password": "wrong-password"},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": token},
    )
    assert response.status_code == 401

    async for session in session_for(clean_database):
        reset_attempts = await session.scalar(
            text("SELECT attempts FROM auth_throttles WHERE key_hash = :key_hash"),
            {"key_hash": email_key_hash},
        )

    assert reset_attempts == 1
