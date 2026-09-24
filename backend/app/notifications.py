"""Reply notifications: durable delivery records, retries, and preferences.

In-app live updates are already P0. This adds email for agent public replies (to the
customer) and customer public replies (to the assigned agent). Rules:

- Never notify the author. Internal messages never enter the customer email path.
- Unassigned-ticket customer replies record a queue notification, not a mass email.
- Delivery records are unique per logical message/recipient/channel, so replaying a
  message never creates duplicates; an email outage never rolls back the reply.
- Emails contain the ticket number, generic update text, and a login-required link;
  never the body, attachments, or internal content.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.dependencies import get_current_user, get_db, require_staff
from app.config import Settings
from app.errors import ApiError
from app.models import (
    NotificationDelivery,
    NotificationPreference,
    Role,
    Ticket,
    TicketMessage,
    User,
    Visibility,
)
from app.mutations import Writer

logger = logging.getLogger("app.notifications")
router = APIRouter(prefix="/api/v1", tags=["notifications"])


def render_email(ticket: Ticket, settings: Settings) -> tuple[str, str]:
    """Generic, login-required notification; no body, attachment, or internal data."""
    subject = f"New reply on {ticket.ticket_number}"
    link = f"{settings.public_base_url.rstrip('/')}/login"
    body = (
        f"There is a new reply on support ticket {ticket.ticket_number}.\n\n"
        f"Sign in to view and respond: {link}\n\n"
        "For your security, message contents are not included in this email."
    )
    return subject, body


def _send_email(settings: Settings, to_email: str, subject: str, body: str) -> str:
    backend = settings.email_backend
    if backend == "disabled":
        raise EmailDisabled("email backend is disabled")
    if backend == "console":
        # Redacted: recipient and ticket subject only, never the body or message text.
        logger.info("email.notification", extra={"path": f"to={to_email} subject={subject}"})
        return "console"
    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=5) as client:
        client.ehlo()
        client.send_message(message)
    return message.get("Message-ID") or "smtp"


class EmailDisabled(RuntimeError):
    pass


async def enqueue_for_message(
    db: AsyncSession, ticket: Ticket, message: TicketMessage, settings: Settings
) -> None:
    if message.visibility != Visibility.PUBLIC.value:
        return
    if message.sender_role_snapshot == Role.CUSTOMER.value:
        if ticket.assigned_agent_id is None:
            # Queue notification rather than emailing every agent.
            await _insert_delivery(db, ticket.id, message.id, None, "queue")
            return
        recipient_id = ticket.assigned_agent_id
    else:
        recipient_id = ticket.customer_id
    if recipient_id == message.sender_id:
        return  # never notify the author
    await _insert_delivery(db, ticket.id, message.id, recipient_id, "email")


async def _insert_delivery(
    db: AsyncSession,
    ticket_id: UUID,
    message_id: UUID,
    recipient_id: UUID | None,
    channel: str,
) -> None:
    now = datetime.now(UTC)
    statement = pg_insert(NotificationDelivery).values(
        ticket_id=ticket_id,
        message_id=message_id,
        recipient_id=recipient_id,
        channel=channel,
        status="PENDING",
        attempts=0,
        available_at=now,
        created_at=now,
    )
    # Duplicate logical notifications are ignored, never doubled.
    await db.execute(statement.on_conflict_do_nothing())


async def process_deliveries(
    db: AsyncSession, settings: Settings, now: datetime, limit: int = 50
) -> list[str]:
    """Deliver due notifications. Returns a status per processed row (for tests)."""
    rows = list(
        await db.scalars(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.status == "PENDING",
                NotificationDelivery.available_at <= now,
            )
            .order_by(NotificationDelivery.created_at, NotificationDelivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    outcomes: list[str] = []
    for delivery in rows:
        if delivery.channel == "queue":
            delivery.status = "SENT"
            delivery.sent_at = now
            delivery.provider_reference = "queue"
            outcomes.append("queue")
            continue

        recipient = await db.get(User, delivery.recipient_id) if delivery.recipient_id else None
        if recipient is None or not recipient.is_active:
            delivery.status = "SKIPPED"
            delivery.last_error = "recipient_ineligible"
            outcomes.append("skipped")
            continue
        preference = await db.get(NotificationPreference, recipient.id)
        if preference is not None and not preference.email_on_reply:
            delivery.status = "SKIPPED"
            delivery.last_error = "preference_disabled"
            outcomes.append("skipped")
            continue

        ticket = await db.get(Ticket, delivery.ticket_id)
        if ticket is None:
            delivery.status = "SKIPPED"
            delivery.last_error = "ticket_missing"
            outcomes.append("skipped")
            continue
        subject, body = render_email(ticket, settings)
        try:
            reference = _send_email(settings, recipient.email, subject, body)
        except EmailDisabled:
            delivery.status = "SKIPPED"
            delivery.last_error = "email_disabled"
            outcomes.append("skipped")
        except Exception:
            delivery.attempts += 1
            delivery.last_error = "send_failed"
            if delivery.attempts >= settings.notification_max_attempts:
                delivery.status = "FAILED"
                outcomes.append("failed")
            else:
                delivery.available_at = now + timedelta(seconds=min(300, 2**delivery.attempts))
                outcomes.append("retry")
        else:
            delivery.status = "SENT"
            delivery.sent_at = now
            delivery.provider_reference = reference
            delivery.last_error = None
            outcomes.append("sent")
    return outcomes


async def process_batch(factory: async_sessionmaker[AsyncSession], settings: Settings) -> int:
    async with factory() as db, db.begin():
        outcomes = await process_deliveries(db, settings, datetime.now(UTC))
    return len(outcomes)


# --- API ---------------------------------------------------------------------


class PreferenceDTO(BaseModel):
    email_on_reply: bool


class DeliveryDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    message_id: UUID
    recipient_id: UUID | None
    channel: str
    status: str
    attempts: int
    last_error: str | None


class DeliveryList(BaseModel):
    items: list[DeliveryDTO]


@router.get("/notification-preferences", response_model=PreferenceDTO)
async def get_preferences(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PreferenceDTO:
    preference = await db.get(NotificationPreference, user.id)
    return PreferenceDTO(email_on_reply=preference.email_on_reply if preference else True)


@router.patch("/notification-preferences", response_model=PreferenceDTO)
async def update_preferences(
    payload: PreferenceDTO,
    user: Writer,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PreferenceDTO:
    now = datetime.now(UTC)
    statement = pg_insert(NotificationPreference).values(
        user_id=user.id, email_on_reply=payload.email_on_reply, updated_at=now
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[NotificationPreference.user_id],
            set_={"email_on_reply": payload.email_on_reply, "updated_at": now},
        )
    )
    await db.commit()
    return PreferenceDTO(email_on_reply=payload.email_on_reply)


@router.get("/notifications/failed", response_model=DeliveryList)
async def failed_deliveries(
    _user: Annotated[User, Depends(require_staff)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeliveryList:
    rows = list(
        await db.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.status == "FAILED")
            .order_by(NotificationDelivery.created_at.desc())
            .limit(100)
        )
    )
    return DeliveryList(items=[DeliveryDTO.model_validate(row) for row in rows])


@router.post("/notifications/{delivery_id}/retry", response_model=DeliveryDTO)
async def retry_delivery(
    delivery_id: UUID,
    _user: Writer,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeliveryDTO:
    delivery = await db.get(NotificationDelivery, delivery_id)
    if delivery is None:
        raise ApiError(404, "not_found", "Resource not found.")
    if delivery.status not in {"FAILED", "SKIPPED"}:
        raise ApiError(409, "not_retryable", "Only failed or skipped deliveries can be retried.")
    delivery.status = "PENDING"
    delivery.attempts = 0
    delivery.last_error = None
    delivery.available_at = datetime.now(UTC)
    await db.commit()
    return DeliveryDTO.model_validate(delivery)


def utcnow() -> datetime:
    return datetime.now(UTC)
