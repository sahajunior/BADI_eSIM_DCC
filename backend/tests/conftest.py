"""Shared Phase 1 integration fixtures.

These fixtures deliberately guard the database name before running migrations or
truncation so integration tests cannot mutate a developer database by accident.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Must be set before app imports so Settings has a stable test-only auth secret.
os.environ.setdefault(
    "BADI_AUTH_SECRET",
    "phase1-test-secret-not-for-production-0123456789abcdef",
)

TEST_ORIGIN = "http://127.0.0.1:8080"
TEST_PASSWORD = "Correct Horse Battery Staple 2026!"


@dataclass(frozen=True)
class SeededUsers:
    customer_a_id: UUID
    customer_b_id: UUID
    agent_a_id: UUID
    agent_b_id: UUID
    admin_id: UUID
    customer_a_email: str = "customer.a@example.test"
    customer_b_email: str = "customer.b@example.test"
    agent_a_email: str = "agent.a@example.test"
    agent_b_email: str = "agent.b@example.test"
    admin_email: str = "admin@example.test"
    password: str = TEST_PASSWORD


@dataclass(frozen=True)
class SeededTickets:
    customer_a_ticket_id: UUID
    customer_b_ticket_id: UUID


@dataclass(frozen=True)
class SeedData:
    users: SeededUsers
    tickets: SeededTickets


@pytest.fixture(scope="session")
def migrated_database_url() -> Iterator[str]:
    database_url = os.environ.get("BADI_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("BADI_TEST_DATABASE_URL is required for PostgreSQL integration tests")

    _assert_test_database(database_url)
    backend_dir = Path(__file__).resolve().parents[1]
    alembic_ini = backend_dir / "alembic.ini"

    env = os.environ.copy()
    env["BADI_DATABASE_URL"] = database_url
    env["BADI_TEST_DATABASE_URL"] = database_url
    env.setdefault("BADI_ENVIRONMENT", "test")
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(alembic_ini), "upgrade", "head"],
        cwd=backend_dir,
        env=env,
        check=True,
    )
    yield database_url


@pytest.fixture
async def clean_database(migrated_database_url: str) -> AsyncIterator[str]:
    await _truncate_known_tables(migrated_database_url)
    try:
        yield migrated_database_url
    finally:
        await _truncate_known_tables(migrated_database_url)


@pytest.fixture
async def seeded_data(clean_database: str) -> SeedData:
    from app.auth.passwords import hash_password
    from app.models import (
        Role,
        Ticket,
        TicketCategory,
        TicketPriority,
        TicketSlaCycle,
        TicketStatus,
        User,
    )
    from app.sla import POLICY_VERSION, first_response_target, resolution_target

    engine = create_async_engine(clean_database, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as session:
            customer_a = User(
                email="customer.a@example.test",
                display_name="Customer Alpha",
                password_hash=hash_password(TEST_PASSWORD),
                role=Role.CUSTOMER.value,
                is_active=True,
            )
            customer_b = User(
                email="customer.b@example.test",
                display_name="Customer Beta",
                password_hash=hash_password(TEST_PASSWORD),
                role=Role.CUSTOMER.value,
                is_active=True,
            )
            agent_a = User(
                email="agent.a@example.test",
                display_name="Agent Alpha",
                password_hash=hash_password(TEST_PASSWORD),
                role=Role.AGENT.value,
                is_active=True,
            )
            agent_b = User(
                email="agent.b@example.test",
                display_name="Agent Beta",
                password_hash=hash_password(TEST_PASSWORD),
                role=Role.AGENT.value,
                is_active=True,
            )
            admin = User(
                email="admin@example.test",
                display_name="Admin Ada",
                password_hash=hash_password(TEST_PASSWORD),
                role=Role.ADMIN.value,
                is_active=True,
            )
            session.add_all([customer_a, customer_b, agent_a, agent_b, admin])
            await session.flush()

            ticket_a = Ticket(
                ticket_sequence=1001,
                customer_id=customer_a.id,
                customer_email_snapshot=customer_a.email,
                created_by_id=customer_a.id,
                subject="Customer A eSIM activation issue",
                description="The QR code fails during activation.",
                category=TicketCategory.ACTIVATION.value,
                priority=TicketPriority.HIGH.value,
                status=TicketStatus.OPEN.value,
                assigned_agent_id=agent_a.id,
            )
            ticket_b = Ticket(
                ticket_sequence=1002,
                customer_id=customer_b.id,
                customer_email_snapshot=customer_b.email,
                created_by_id=customer_b.id,
                subject="Customer B roaming issue",
                description="Roaming data is not available.",
                category=TicketCategory.CONNECTIVITY.value,
                priority=TicketPriority.MEDIUM.value,
                status=TicketStatus.OPEN.value,
                assigned_agent_id=agent_b.id,
            )
            session.add_all([ticket_a, ticket_b])
            await session.commit()
            created = datetime.now(UTC)
            session.add_all(
                [
                    TicketSlaCycle(
                        ticket_id=ticket.id,
                        cycle_number=1,
                        policy_version=POLICY_VERSION,
                        priority=ticket.priority,
                        first_response_due_at=created
                        + timedelta(seconds=first_response_target(ticket.priority)),
                        resolution_started_at=created,
                        resolution_due_at=created
                        + timedelta(seconds=resolution_target(ticket.priority)),
                        is_active=True,
                        created_at=created,
                    )
                    for ticket in (ticket_a, ticket_b)
                ]
            )
            await session.commit()
            await session.execute(text("SELECT setval('ticket_sequence_seq', 1002, true)"))
            await session.commit()

            return SeedData(
                users=SeededUsers(
                    customer_a_id=customer_a.id,
                    customer_b_id=customer_b.id,
                    agent_a_id=agent_a.id,
                    agent_b_id=agent_b.id,
                    admin_id=admin.id,
                ),
                tickets=SeededTickets(
                    customer_a_ticket_id=ticket_a.id,
                    customer_b_ticket_id=ticket_b.id,
                ),
            )
    finally:
        await engine.dispose()


@pytest.fixture
async def app_client(clean_database: str) -> AsyncIterator[AsyncClient]:
    async with _lifespan_client(clean_database) as client:
        yield client


@pytest.fixture
async def seeded_app_client(
    seeded_data: SeedData,
    clean_database: str,
) -> AsyncIterator[AsyncClient]:
    async with _lifespan_client(clean_database) as client:
        client.seeded_data = seeded_data  # type: ignore[attr-defined]
        yield client


@asynccontextmanager
async def _lifespan_client(database_url: str) -> AsyncIterator[AsyncClient]:
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        database_url=database_url,
        environment="test",
        allowed_origins=[TEST_ORIGIN],
        attachment_dir=tempfile.mkdtemp(prefix="badi-attachments-"),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
    assert getattr(app.state, "db_engine", None) is None


async def _truncate_known_tables(database_url: str) -> None:
    _assert_test_database(database_url)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE TABLE "
                    "idempotency_records, ticket_sla_cycles, outbox_events, ticket_events, "
                    "ticket_messages, saved_replies, user_ticket_state, notification_deliveries, "
                    "notification_preferences, attachments, auth_sessions, auth_throttles, "
                    "tickets, users "
                    "RESTART IDENTITY CASCADE"
                )
            )
    finally:
        await engine.dispose()


def _assert_test_database(database_url: str) -> None:
    parsed = urlsplit(database_url)
    database_name = parsed.path.rsplit("/", maxsplit=1)[-1]
    if not database_name.endswith("_test"):
        raise RuntimeError(
            f"Refusing to run integration tests against non-test database {database_name!r}; "
            "BADI_TEST_DATABASE_URL database name must end with '_test'."
        )


async def session_for(database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as session:
            yield session
    finally:
        await engine.dispose()
