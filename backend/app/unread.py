"""Private per-user unread markers.

These are not read receipts: a marker is only ever visible to the user it belongs
to, and no activity is broadcast when a ticket is marked seen. Unread counts
exclude a user's own messages, and for customers they exclude INTERNAL messages
entirely. When a user has no marker, the ticket's creation time is the baseline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, Ticket, TicketMessage, User, UserTicketState, Visibility


async def mark_seen(db: AsyncSession, user: User, ticket_id: UUID, now: datetime) -> None:
    statement = pg_insert(UserTicketState).values(
        user_id=user.id, ticket_id=ticket_id, last_seen_at=now
    )
    statement = statement.on_conflict_do_update(
        index_elements=[UserTicketState.user_id, UserTicketState.ticket_id],
        set_={"last_seen_at": now, "updated_at": now},
    )
    await db.execute(statement)


async def unread_counts(db: AsyncSession, user: User, tickets: list[Ticket]) -> dict[UUID, int]:
    """Return {ticket_id: unread_count} for the given tickets and this user."""
    if not tickets:
        return {}
    by_id = {ticket.id: ticket for ticket in tickets}
    statement = (
        select(TicketMessage.ticket_id, func.count())
        .select_from(TicketMessage)
        .join(Ticket, Ticket.id == TicketMessage.ticket_id)
        .outerjoin(
            UserTicketState,
            and_(
                UserTicketState.ticket_id == TicketMessage.ticket_id,
                UserTicketState.user_id == user.id,
            ),
        )
        .where(
            TicketMessage.ticket_id.in_(list(by_id)),
            TicketMessage.sender_id != user.id,
            TicketMessage.created_at
            > func.coalesce(UserTicketState.last_seen_at, Ticket.created_at),
        )
        .group_by(TicketMessage.ticket_id)
    )
    if user.role == Role.CUSTOMER:
        statement = statement.where(TicketMessage.visibility == Visibility.PUBLIC.value)
    rows = await db.execute(statement)
    return {ticket_id: int(count) for ticket_id, count in rows.all()}


def utcnow() -> datetime:
    return datetime.now(UTC)
