"""Immutable conversations with query-time audience filters and atomic side effects."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.attachments import AttachmentDTO, attachments_for, bind_attachments
from app.auth.dependencies import get_current_user
from app.errors import ApiError
from app.models import OutboxEvent, Role, Ticket, TicketEvent, TicketMessage, User, Visibility
from app.mutations import Db, Writer, begin_idempotency, finish_idempotency
from app.notifications import enqueue_for_message
from app.sla import apply_status_change, record_first_public_response
from app.tickets import get_visible_ticket, visible_tickets

router = APIRouter(prefix="/api/v1/tickets", tags=["messages"])


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
    visibility: Visibility = Visibility.PUBLIC
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=3)


class PublicMessage(BaseModel):
    id: UUID
    ticket_id: UUID
    body: str
    sender_type: str
    display_name: str
    created_at: datetime
    attachments: list[AttachmentDTO] = Field(default_factory=list)


class StaffMessage(PublicMessage):
    sender_id: UUID
    sender_role_snapshot: str
    visibility: str
    position: int


class MessagePage(BaseModel):
    items: list[StaffMessage | PublicMessage]
    older_cursor: UUID | None
    newer_cursor: UUID | None
    has_more: bool


def visible_messages(user: User, ticket_id: UUID) -> Select[tuple[TicketMessage]]:
    query = select(TicketMessage).where(TicketMessage.ticket_id == ticket_id)
    if user.role == Role.CUSTOMER:
        query = query.where(TicketMessage.visibility == Visibility.PUBLIC)
    return query


def project_message(
    message: TicketMessage,
    user: User,
    attachments: list[AttachmentDTO] | None = None,
) -> PublicMessage | StaffMessage:
    # Public support identity is deliberately stable; no staff email, role, ID or
    # private sequence gap is included in a customer projection.
    public = PublicMessage(
        id=message.id,
        ticket_id=message.ticket_id,
        body=message.body,
        sender_type="CUSTOMER" if message.sender_role_snapshot == Role.CUSTOMER else "SUPPORT",
        display_name="Customer" if message.sender_role_snapshot == Role.CUSTOMER else "Support",
        created_at=message.created_at,
        attachments=attachments or [],
    )
    if user.role == Role.CUSTOMER:
        return public
    return StaffMessage(
        **public.model_dump(),
        sender_id=message.sender_id,
        sender_role_snapshot=message.sender_role_snapshot,
        visibility=message.visibility,
        position=message.position,
    )


async def visible_message(
    db: AsyncSession, user: User, ticket_id: UUID, message_id: UUID
) -> TicketMessage:
    message = await db.scalar(
        visible_messages(user, ticket_id).where(TicketMessage.id == message_id)
    )
    if message is None:
        raise ApiError(404, "not_found", "Resource not found.")
    return message


@router.get("/{ticket_id}/messages", response_model=MessagePage)
async def list_messages(
    ticket_id: UUID,
    db: Db,
    user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: UUID | None = None,
    after: UUID | None = None,
) -> MessagePage:
    await get_visible_ticket(db, user, ticket_id)
    if before is not None and after is not None:
        raise ApiError(422, "invalid_cursor", "Use either before or after, not both.")
    query = visible_messages(user, ticket_id)
    marker_id = before or after
    if marker_id is not None:
        marker = await visible_message(db, user, ticket_id, marker_id)
        query = query.where(
            TicketMessage.position < marker.position
            if before is not None
            else TicketMessage.position > marker.position
        )
    query = query.order_by(
        TicketMessage.position.asc() if after is not None else TicketMessage.position.desc()
    )
    rows = list(await db.scalars(query.limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    if after is None:
        rows.reverse()
    grouped = await attachments_for(db, [row.id for row in rows])
    return MessagePage(
        items=[project_message(row, user, grouped.get(row.id)) for row in rows],
        older_cursor=rows[0].id if rows else None,
        newer_cursor=rows[-1].id if rows else after,
        has_more=has_more,
    )


@router.post("/{ticket_id}/messages", status_code=201, response_model=StaffMessage | PublicMessage)
async def create_message(
    ticket_id: UUID,
    payload: MessageInput,
    request: Request,
    response: Response,
    db: Db,
    user: Writer,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> PublicMessage | StaffMessage:
    route = f"/tickets/{ticket_id}/messages"
    content = payload.model_dump(mode="json")
    record = await begin_idempotency(db, user.id, route, idempotency_key, content)
    ticket = await db.scalar(
        visible_tickets(user)
        .where(Ticket.id == ticket_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if ticket is None:
        raise ApiError(404, "not_found", "Resource not found.")
    if user.role == Role.CUSTOMER and payload.visibility != Visibility.PUBLIC:
        raise ApiError(403, "forbidden", "Customers may only send public replies.")
    if record is not None:
        message = await visible_message(db, user, ticket_id, record.result_id)
        response.headers["Idempotency-Replayed"] = "true"
        return project_message(message, user)
    if ticket.status == "CLOSED" and payload.visibility == Visibility.PUBLIC:
        raise ApiError(409, "ticket_closed", "Reopen the ticket before adding a public reply.")
    position = (
        await db.scalar(
            select(func.coalesce(func.max(TicketMessage.position), 0)).where(
                TicketMessage.ticket_id == ticket_id
            )
        )
    ) or 0
    now = datetime.now(UTC)
    message = TicketMessage(
        ticket_id=ticket_id,
        sender_id=user.id,
        sender_role_snapshot=user.role,
        visibility=payload.visibility.value,
        body=payload.body,
        position=position + 1,
        created_at=now,
    )
    db.add(message)
    await db.flush()
    await bind_attachments(db, user, ticket_id, message.id, payload.attachment_ids, now)
    ticket.version += 1
    ticket.updated_at = now
    if payload.visibility == Visibility.PUBLIC:
        ticket.public_updated_at = now
        if user.role != Role.CUSTOMER and ticket.first_agent_response_at is None:
            ticket.first_agent_response_at = now
        if user.role != Role.CUSTOMER:
            await record_first_public_response(db, ticket, now)
        if user.role == Role.CUSTOMER and ticket.status in {"WAITING_FOR_CUSTOMER", "RESOLVED"}:
            old_status = ticket.status
            ticket.status = "IN_PROGRESS"
            ticket.resolved_at = None
            ticket.closed_at = None
            await apply_status_change(db, ticket, old_status, "IN_PROGRESS", now)
            db.add(
                TicketEvent(
                    ticket_id=ticket_id,
                    actor_id=None,
                    event_type="status.changed",
                    field="status",
                    old_value=old_status,
                    new_value=ticket.status,
                    reason="customer_reply",
                    source_message_id=message.id,
                    request_id=request.state.request_id,
                    visibility="PUBLIC",
                )
            )
    db.add(
        TicketEvent(
            ticket_id=ticket_id,
            actor_id=user.id,
            event_type="message.created",
            source_message_id=message.id,
            request_id=request.state.request_id,
            visibility=payload.visibility.value,
        )
    )
    db.add(
        OutboxEvent(ticket_id=ticket_id, kind="ticket.changed", visibility=payload.visibility.value)
    )
    await enqueue_for_message(db, ticket, message, request.app.state.settings)
    assert idempotency_key is not None
    finish_idempotency(db, user.id, route, idempotency_key, content, "message", message.id)
    grouped = await attachments_for(db, [message.id])
    result = project_message(message, user, grouped.get(message.id))
    await db.commit()
    return result
