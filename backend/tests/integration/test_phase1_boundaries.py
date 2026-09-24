"""Security and migration boundary cases against the isolated PostgreSQL service."""

import asyncio
from uuid import UUID

import pytest
from conftest import TEST_ORIGIN, TEST_PASSWORD, SeedData, session_for
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.passwords import verify_password
from app.migrate import grant_runtime_privileges
from app.models import AuthSession, Role, User
from app.provision import canonical_email, create_user
from app.seed import seed_demo


async def test_concurrent_login_cannot_reuse_revoked_bootstrap(
    seeded_app_client: AsyncClient, seeded_data: SeedData, clean_database: str
) -> None:
    csrf = (await seeded_app_client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    old_cookie = seeded_app_client.cookies["badi_session"]
    headers = {
        "Origin": TEST_ORIGIN,
        "X-CSRF-Token": csrf,
        "Cookie": f"badi_session={old_cookie}",
    }
    responses = await asyncio.gather(
        *(
            seeded_app_client.post(
                "/api/v1/auth/login",
                json={"email": seeded_data.users.customer_a_email, "password": TEST_PASSWORD},
                headers=headers,
            )
            for _ in range(2)
        )
    )
    assert sorted(response.status_code for response in responses) == [200, 403]
    async for session in session_for(clean_database):
        active = list(
            await session.scalars(
                select(AuthSession).where(
                    AuthSession.user_id == seeded_data.users.customer_a_id,
                    AuthSession.revoked_at.is_(None),
                )
            )
        )
        assert len(active) == 1


async def test_malformed_referer_denied_without_server_error(
    seeded_app_client: AsyncClient, seeded_data: SeedData
) -> None:
    csrf = (await seeded_app_client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await seeded_app_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_data.users.customer_a_email, "password": TEST_PASSWORD},
        headers={"Referer": "http://[invalid", "X-CSRF-Token": csrf},
    )
    assert response.status_code == 403
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"


async def test_bootstrap_rate_limit_is_database_backed(app_client: AsyncClient) -> None:
    statuses = [(await app_client.get("/api/v1/auth/csrf")).status_code for _ in range(61)]
    assert statuses[:60] == [200] * 60
    assert statuses[60] == 429


async def test_seed_is_idempotent_and_preserves_passwords(clean_database: str) -> None:
    async for session in session_for(clean_database):
        assert await seed_demo(session, TEST_PASSWORD) == (5, 7)
        await session.commit()
        first = {
            email: hashed
            for email, hashed in await session.execute(select(User.email, User.password_hash))
        }
        assert await seed_demo(session, "A different demo password!") == (0, 0)
        await session.commit()
        second = {
            email: hashed
            for email, hashed in await session.execute(select(User.email, User.password_hash))
        }
        assert first == second


async def test_database_rejects_invalid_roles(clean_database: str) -> None:
    async for session in session_for(clean_database):
        session.add(
            User(
                email="invalid@example.test",
                display_name="Invalid",
                password_hash="irrelevant-test-hash",
                role="SUPERADMIN",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_provision_creates_hashed_account_and_refuses_duplicates(clean_database: str) -> None:
    async for session in session_for(clean_database):
        email = canonical_email("  New.Customer@Example.test ")
        user = await create_user(
            session, email=email, name="New Customer", role=Role.CUSTOMER, password=TEST_PASSWORD
        )
        assert user.email == "new.customer@example.test"
        assert user.role == "CUSTOMER"
        assert user.is_active
        assert user.password_hash != TEST_PASSWORD
        assert verify_password(TEST_PASSWORD, user.password_hash)
        original_hash = user.password_hash
        with pytest.raises(ValueError, match="already exists"):
            await create_user(
                session, email=email, name="Replacement", role=Role.ADMIN, password="changed"
            )
        await session.refresh(user)
        assert user.role == "CUSTOMER"
        assert user.display_name == "New Customer"
        assert user.password_hash == original_hash


@pytest.mark.parametrize("conflict", ["role", "inactive"])
async def test_seed_refuses_conflicting_accounts(clean_database: str, conflict: str) -> None:
    async for session in session_for(clean_database):
        await seed_demo(session, TEST_PASSWORD)
        await session.commit()
        user = await session.scalar(select(User).where(User.email == "customer1@example.test"))
        assert user is not None
        if conflict == "role":
            user.role = "ADMIN"
        else:
            user.is_active = False
        await session.commit()
        with pytest.raises(ValueError, match="conflicting role or is inactive"):
            await seed_demo(session, TEST_PASSWORD)
        await session.rollback()


async def test_runtime_role_can_read_but_not_migrate(clean_database: str) -> None:
    password = "integration-runtime-password-only"
    admin = create_async_engine(clean_database)
    try:
        await grant_runtime_privileges(admin, password)
    finally:
        await admin.dispose()
    runtime_url = make_url(clean_database).set(username="badi_app", password=password)
    engine = create_async_engine(runtime_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT current_user")) == "badi_app"
            assert await connection.scalar(
                text("SELECT has_table_privilege(current_user, 'users', 'SELECT')")
            )
            assert not await connection.scalar(
                text("SELECT has_table_privilege(current_user, 'alembic_version', 'UPDATE')")
            )
            assert not await connection.scalar(
                text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            )
            assert not await connection.scalar(
                text(
                    "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
    finally:
        await engine.dispose()


async def test_customer_query_is_scoped_even_without_http(
    seeded_data: SeedData, clean_database: str
) -> None:
    from app.errors import ApiError
    from app.tickets import get_visible_ticket, visible_tickets

    async for session in session_for(clean_database):
        customer = await session.get(User, seeded_data.users.customer_a_id)
        assert customer is not None
        tickets = list(await session.scalars(visible_tickets(customer)))
        assert [ticket.id for ticket in tickets] == [seeded_data.tickets.customer_a_ticket_id]
        with pytest.raises(ApiError) as denied:
            await get_visible_ticket(session, customer, seeded_data.tickets.customer_b_ticket_id)
        assert denied.value.status_code == 404
        assert isinstance(customer.id, UUID)
        customer.is_active = False
        with pytest.raises(ApiError) as inactive:
            visible_tickets(customer)
        assert inactive.value.status_code == 401
