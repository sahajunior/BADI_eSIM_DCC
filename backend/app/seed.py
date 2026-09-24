from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.config import Settings
from app.db import create_engine, dispose_engine
from app.models import (
    Role,
    SavedReply,
    Ticket,
    TicketCategory,
    TicketEvent,
    TicketMessage,
    TicketPriority,
    TicketSlaCycle,
    TicketStatus,
    User,
    Visibility,
)
from app.provision import canonical_email
from app.sla import POLICY_VERSION, first_response_target, resolution_target

SUCCESS = 0
VALIDATION_ERROR = 2
DATABASE_ERROR = 4
DEMO_NAMESPACE = uuid.UUID("a1d93d5d-4bea-48c8-8e82-c03c2d669b0b")


@dataclass(frozen=True)
class DemoUser:
    email: str
    display_name: str
    role: Role


@dataclass(frozen=True)
class DemoMessage:
    key: str
    sender_email: str
    visibility: Visibility
    body: str


@dataclass(frozen=True)
class DemoTicket:
    key: str
    customer_email: str
    category: TicketCategory
    subject: str
    description: str
    priority: TicketPriority
    status: TicketStatus
    order_id: str | None = None
    assigned_agent_email: str | None = None
    created_by_email: str | None = None
    messages: tuple[DemoMessage, ...] = ()


DEMO_USERS: tuple[DemoUser, ...] = (
    DemoUser("customer1@example.test", "Customer One", Role.CUSTOMER),
    DemoUser("customer2@example.test", "Customer Two", Role.CUSTOMER),
    DemoUser("agent1@example.test", "Agent One", Role.AGENT),
    DemoUser("agent2@example.test", "Agent Two", Role.AGENT),
    DemoUser("admin@example.test", "Admin User", Role.ADMIN),
)


DEMO_TICKETS: tuple[DemoTicket, ...] = (
    DemoTicket(
        key="customer1-connectivity",
        customer_email="customer1@example.test",
        order_id="ORD-DEMO-1001",
        category=TicketCategory.CONNECTIVITY,
        subject="eSIM installed but no internet",
        description="The eSIM installs successfully, but mobile data never connects.",
        priority=TicketPriority.HIGH,
        status=TicketStatus.IN_PROGRESS,
        assigned_agent_email="agent1@example.test",
        messages=(
            DemoMessage(
                "reply-1",
                "agent1@example.test",
                Visibility.PUBLIC,
                "Thanks for reaching out. Are you currently travelling in Turkey?",
            ),
            DemoMessage(
                "reply-2",
                "customer1@example.test",
                Visibility.PUBLIC,
                "Yes, I am in Istanbul and mobile data still shows no connection.",
            ),
            DemoMessage(
                "note-1",
                "agent1@example.test",
                Visibility.INTERNAL,
                "Provider ticket PRV-7781 raised. Do not share this reference with the customer.",
            ),
        ),
    ),
    DemoTicket(
        key="customer2-activation",
        customer_email="customer2@example.test",
        order_id="ORD-DEMO-1002",
        category=TicketCategory.ACTIVATION,
        subject="Activation QR code already used",
        description="The activation screen says the QR code has already been used.",
        priority=TicketPriority.MEDIUM,
        status=TicketStatus.WAITING_FOR_CUSTOMER,
        assigned_agent_email="agent2@example.test",
        messages=(
            DemoMessage(
                "reply-1",
                "agent2@example.test",
                Visibility.PUBLIC,
                "Could you confirm the destination and package shown in your order?",
            ),
        ),
    ),
    DemoTicket(
        key="customer1-installation",
        customer_email="customer1@example.test",
        category=TicketCategory.INSTALLATION,
        subject="Cannot scan the eSIM QR code",
        description="The camera will not recognise the QR code from the confirmation email.",
        priority=TicketPriority.LOW,
        status=TicketStatus.OPEN,
    ),
    DemoTicket(
        key="customer2-order",
        customer_email="customer2@example.test",
        order_id="ORD-DEMO-1004",
        category=TicketCategory.ORDER,
        subject="Order still pending after payment",
        description="Payment succeeded but the order page still shows pending.",
        priority=TicketPriority.URGENT,
        status=TicketStatus.IN_PROGRESS,
        assigned_agent_email="agent1@example.test",
        created_by_email="agent1@example.test",
        messages=(
            DemoMessage(
                "reply-1",
                "agent1@example.test",
                Visibility.PUBLIC,
                "We can see your payment and are checking the order status now.",
            ),
        ),
    ),
    DemoTicket(
        key="customer1-topup",
        customer_email="customer1@example.test",
        order_id="ORD-DEMO-1005",
        category=TicketCategory.TOPUP,
        subject="Top-up not reflected in balance",
        description="A 5 GB top-up was purchased an hour ago and the balance is unchanged.",
        priority=TicketPriority.HIGH,
        status=TicketStatus.WAITING_FOR_PROVIDER,
        assigned_agent_email="agent2@example.test",
        messages=(
            DemoMessage(
                "note-1",
                "agent2@example.test",
                Visibility.INTERNAL,
                "Carrier top-up batch delayed; monitoring before replying to the customer.",
            ),
        ),
    ),
    DemoTicket(
        key="customer2-refund",
        customer_email="customer2@example.test",
        category=TicketCategory.REFUND,
        subject="Requesting a refund for unused data",
        description="The package was never activated; please refund the purchase.",
        priority=TicketPriority.MEDIUM,
        status=TicketStatus.RESOLVED,
        assigned_agent_email="agent1@example.test",
        messages=(
            DemoMessage(
                "reply-1",
                "agent1@example.test",
                Visibility.PUBLIC,
                "Your refund is approved and should appear within 5-7 business days.",
            ),
        ),
    ),
    DemoTicket(
        key="customer2-other",
        customer_email="customer2@example.test",
        category=TicketCategory.OTHER,
        subject="Does my plan support roaming?",
        description="I want to know whether roaming is included before I travel.",
        priority=TicketPriority.LOW,
        status=TicketStatus.CLOSED,
        assigned_agent_email="agent2@example.test",
        messages=(
            DemoMessage(
                "reply-1",
                "agent1@example.test",
                Visibility.PUBLIC,
                "Roaming is included in your plan for 40 destinations.",
            ),
            DemoMessage(
                "reply-2",
                "customer2@example.test",
                Visibility.PUBLIC,
                "That is exactly what I needed, thank you.",
            ),
            DemoMessage(
                "note-1",
                "agent2@example.test",
                Visibility.INTERNAL,
                "Closing after the customer confirmed the answer.",
            ),
        ),
    ),
)


@dataclass(frozen=True)
class DemoSavedReply:
    key: str
    author_email: str
    title: str
    body: str
    category: TicketCategory | None


DEMO_SAVED_REPLIES: tuple[DemoSavedReply, ...] = (
    DemoSavedReply(
        "connectivity-location",
        "agent1@example.test",
        "Connectivity: confirm location",
        "Thanks for reaching out. To investigate, please confirm your current country and "
        "whether other apps can use mobile data.",
        TicketCategory.CONNECTIVITY,
    ),
    DemoSavedReply(
        "provider-check",
        "agent1@example.test",
        "Provider check in progress",
        "We have raised this with our provider and will update you as soon as we hear back.",
        None,
    ),
)


def demo_uuid(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(DEMO_NAMESPACE, f"{kind}:{key}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Idempotently create local demo users and a full synthetic ticket set.",
        epilog=(
            "Return codes: 0 seeded, 2 invalid flag/environment, 4 database error. "
            "Requires --demo and BADI_DEMO_PASSWORD; never resets existing rows."
        ),
    )
    parser.add_argument(
        "--demo", action="store_true", help="Required safety flag for demo seeding."
    )
    return parser


def demo_password() -> str:
    password = os.environ.get("BADI_DEMO_PASSWORD")
    if password is None:
        raise ValueError("BADI_DEMO_PASSWORD is required for --demo seed")
    if not 12 <= len(password) <= 1024:
        raise ValueError("BADI_DEMO_PASSWORD must be between 12 and 1024 characters")
    return password


async def ensure_user(
    session: AsyncSession, spec: DemoUser, password_hash: str
) -> tuple[User, bool]:
    email = canonical_email(spec.email)
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        if existing.role != spec.role.value or not existing.is_active:
            raise ValueError("existing demo account has a conflicting role or is inactive")
        return existing, False
    user = User(
        id=demo_uuid("user", email),
        email=email,
        display_name=spec.display_name,
        password_hash=password_hash,
        role=spec.role.value,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user, True


async def ensure_ticket(
    session: AsyncSession,
    spec: DemoTicket,
    users_by_email: dict[str, User],
    created_at: datetime,
) -> tuple[Ticket, bool]:
    ticket_id = demo_uuid("ticket", spec.key)
    existing = await session.get(Ticket, ticket_id)
    if existing is not None:
        return existing, False

    customer = users_by_email[canonical_email(spec.customer_email)]
    creator = (
        users_by_email[canonical_email(spec.created_by_email)]
        if spec.created_by_email
        else customer
    )
    assignee = (
        users_by_email[canonical_email(spec.assigned_agent_email)]
        if spec.assigned_agent_email
        else None
    )
    status = spec.status.value
    ticket = Ticket(
        id=ticket_id,
        customer_id=customer.id,
        customer_email_snapshot=customer.email,
        created_by_id=creator.id,
        order_id=spec.order_id,
        category=spec.category.value,
        subject=spec.subject,
        description=spec.description,
        priority=spec.priority.value,
        status=status,
        assigned_agent_id=assignee.id if assignee else None,
        created_at=created_at,
        updated_at=created_at,
        public_updated_at=created_at,
        resolved_at=created_at if spec.status is TicketStatus.RESOLVED else None,
        closed_at=created_at if spec.status is TicketStatus.CLOSED else None,
    )
    session.add(ticket)
    await session.flush()

    events: list[TicketEvent] = [
        TicketEvent(
            id=demo_uuid("event", f"{spec.key}:created"),
            ticket_id=ticket_id,
            actor_id=creator.id,
            event_type="ticket.created",
            field="status",
            old_value=None,
            new_value=TicketStatus.OPEN.value,
            visibility=Visibility.PUBLIC.value,
            created_at=created_at,
        )
    ]
    if spec.status is not TicketStatus.OPEN:
        events.append(
            TicketEvent(
                id=demo_uuid("event", f"{spec.key}:status"),
                ticket_id=ticket_id,
                actor_id=creator.id,
                event_type="status.changed",
                field="status",
                old_value=TicketStatus.OPEN.value,
                new_value=status,
                visibility=Visibility.PUBLIC.value,
                created_at=created_at + timedelta(minutes=1),
            )
        )
    if spec.priority is not TicketPriority.MEDIUM:
        events.append(
            TicketEvent(
                id=demo_uuid("event", f"{spec.key}:priority"),
                ticket_id=ticket_id,
                actor_id=creator.id,
                event_type="priority.changed",
                field="priority",
                old_value=TicketPriority.MEDIUM.value,
                new_value=spec.priority.value,
                visibility=Visibility.PUBLIC.value,
                created_at=created_at + timedelta(minutes=1),
            )
        )
    if assignee is not None:
        events.append(
            TicketEvent(
                id=demo_uuid("event", f"{spec.key}:assigned"),
                ticket_id=ticket_id,
                actor_id=assignee.id,
                event_type="assigned_agent_id.changed",
                field="assigned_agent_id",
                old_value=None,
                new_value=str(assignee.id),
                visibility=Visibility.INTERNAL.value,
                created_at=created_at + timedelta(minutes=1),
            )
        )

    last_public = created_at
    first_agent_response: datetime | None = None
    for position, message_spec in enumerate(spec.messages, start=1):
        sender = users_by_email[canonical_email(message_spec.sender_email)]
        message_at = created_at + timedelta(minutes=position)
        message_id = demo_uuid("message", f"{spec.key}:{message_spec.key}")
        session.add(
            TicketMessage(
                id=message_id,
                ticket_id=ticket_id,
                sender_id=sender.id,
                sender_role_snapshot=sender.role,
                visibility=message_spec.visibility.value,
                body=message_spec.body,
                position=position,
                created_at=message_at,
            )
        )
        events.append(
            TicketEvent(
                id=demo_uuid("event", f"{spec.key}:message:{message_spec.key}"),
                ticket_id=ticket_id,
                actor_id=sender.id,
                event_type="message.created",
                source_message_id=message_id,
                visibility=message_spec.visibility.value,
                created_at=message_at,
            )
        )
        if message_spec.visibility is Visibility.PUBLIC:
            last_public = message_at
            if sender.role != Role.CUSTOMER.value and first_agent_response is None:
                first_agent_response = message_at

    # Messages must exist before their message.created events reference them.
    await session.flush()
    ticket.public_updated_at = last_public
    ticket.updated_at = max(last_public, created_at + timedelta(minutes=len(spec.messages)))
    ticket.first_agent_response_at = first_agent_response
    completed = spec.status in {TicketStatus.RESOLVED, TicketStatus.CLOSED}
    session.add(
        TicketSlaCycle(
            ticket_id=ticket_id,
            cycle_number=1,
            policy_version=POLICY_VERSION,
            priority=spec.priority.value,
            first_response_due_at=created_at
            + timedelta(seconds=first_response_target(spec.priority.value)),
            first_response_at=first_agent_response,
            resolution_started_at=created_at,
            resolution_due_at=created_at
            + timedelta(seconds=resolution_target(spec.priority.value)),
            resolution_completed_at=(ticket.resolved_at or ticket.closed_at) if completed else None,
            is_active=not completed,
            created_at=created_at,
        )
    )
    session.add_all(events)
    await session.flush()
    return ticket, True


async def ensure_saved_reply(
    session: AsyncSession,
    spec: DemoSavedReply,
    users_by_email: dict[str, User],
) -> bool:
    reply_id = demo_uuid("saved_reply", spec.key)
    if await session.get(SavedReply, reply_id) is not None:
        return False
    author = users_by_email[canonical_email(spec.author_email)]
    session.add(
        SavedReply(
            id=reply_id,
            author_id=author.id,
            title=spec.title,
            body=spec.body,
            category=spec.category.value if spec.category else None,
            is_active=True,
        )
    )
    await session.flush()
    return True


async def seed_demo(session: AsyncSession, password: str) -> tuple[int, int]:
    password_hash = hash_password(password)
    users_by_email: dict[str, User] = {}
    created_users = 0
    for user_spec in DEMO_USERS:
        user, created = await ensure_user(session, user_spec, password_hash)
        users_by_email[user.email] = user
        created_users += int(created)

    created_tickets = 0
    base = datetime.now(UTC).replace(microsecond=0)
    total = len(DEMO_TICKETS)
    for index, ticket_spec in enumerate(DEMO_TICKETS):
        created_at = base - timedelta(hours=total - index)
        _ticket, created = await ensure_ticket(session, ticket_spec, users_by_email, created_at)
        created_tickets += int(created)

    for reply_spec in DEMO_SAVED_REPLIES:
        await ensure_saved_reply(session, reply_spec, users_by_email)
    return created_users, created_tickets


async def run(args: argparse.Namespace) -> int:
    if not args.demo:
        print("error: refusing to seed without explicit --demo flag", file=sys.stderr)
        return VALIDATION_ERROR
    try:
        password = demo_password()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return VALIDATION_ERROR

    settings = Settings()
    if settings.environment != "development":
        print("error: demo seed is restricted to development", file=sys.stderr)
        return VALIDATION_ERROR
    engine = create_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            created_users, created_tickets = await seed_demo(session, password)
            await session.commit()
    except IntegrityError:
        print("database error: demo seed conflicted with existing unique data", file=sys.stderr)
        return DATABASE_ERROR
    except Exception as exc:
        print(f"database error: {exc.__class__.__name__}", file=sys.stderr)
        return DATABASE_ERROR
    finally:
        await dispose_engine(engine)

    print(f"demo seed complete: users_created={created_users} tickets_created={created_tickets}")
    return SUCCESS


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
