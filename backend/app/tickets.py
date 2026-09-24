"""Ticket lifecycle, scoped reads, and staff workflow APIs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import Select, String, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, get_db, require_staff
from app.auth.security import client_ip, hit_throttle
from app.errors import ApiError
from app.models import (
    OutboxEvent,
    Role,
    Ticket,
    TicketCategory,
    TicketEvent,
    TicketMessage,
    TicketPriority,
    TicketStatus,
    User,
    Visibility,
)
from app.mutations import Db, Writer, begin_idempotency, finish_idempotency
from app.orders import order_owner
from app.pagination import decode_cursor, encode_cursor, parse_datetime
from app.sla import (
    active_cycle,
    apply_priority_change,
    apply_status_change,
    open_first_cycle,
    sla_projection,
    utcnow,
)
from app.ticket_policy import validate_status_transition
from app.unread import mark_seen, unread_counts

router = APIRouter(prefix="/api/v1", tags=["tickets"])

STATUS_VALUES = {status.value for status in TicketStatus}
PRIORITY_VALUES = {priority.value for priority in TicketPriority}
CATEGORY_VALUES = {category.value for category in TicketCategory}


class CustomerTicket(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_number: str
    subject: str
    description: str
    category: str
    priority: str
    status: str
    order_id: str | None
    created_at: datetime
    public_updated_at: datetime
    unread_count: int = 0


class AgentTicket(CustomerTicket):
    customer_id: UUID
    customer_email_snapshot: str
    created_by_id: UUID
    assigned_agent_id: UUID | None
    version: int
    updated_at: datetime


class TicketList(BaseModel):
    items: list[AgentTicket | CustomerTicket]
    next_cursor: str | None


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_email: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)
    ]
    customer_id: UUID | None = None
    category: TicketCategory
    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)
    ]
    priority: TicketPriority = TicketPriority.MEDIUM
    order_id: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ] = None

    @field_validator("customer_email", mode="after")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        try:
            email = validate_email(value, check_deliverability=False, test_environment=True)
        except EmailNotValidError as exc:
            raise ValueError("valid email address required") from exc
        return email.normalized.lower()


class TicketPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    assigned_agent_id: UUID | None = Field(default=None)
    reason: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ] = None

    @model_validator(mode="after")
    def at_least_one_action(self) -> TicketPatch:
        if "assigned_agent_id" in self.model_fields_set:
            return self
        if self.status is not None or self.priority is not None:
            return self
        if self.reason is not None:
            raise ApiError(422, "empty_patch", "Provide a workflow field to update.")
        raise ApiError(422, "empty_patch", "Provide a workflow field to update.")


class AgentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str


class AgentList(BaseModel):
    items: list[AgentSummary]


class CustomerSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str


class CustomerList(BaseModel):
    items: list[CustomerSummary]
    next_cursor: str | None


class TicketEventDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    field: str | None
    old_value: str | None
    new_value: str | None
    reason: str | None
    actor_id: UUID | None
    source_message_id: UUID | None
    created_at: datetime


class TicketEventPage(BaseModel):
    items: list[TicketEventDTO]
    next_cursor: str | None


class PublicHistoryItem(BaseModel):
    id: UUID
    event_type: str
    field: str | None
    old_value: str | None
    new_value: str | None
    created_at: datetime


class PublicHistoryPage(BaseModel):
    items: list[PublicHistoryItem]
    next_cursor: str | None


class SlaTimerDTO(BaseModel):
    state: str
    target_seconds: int
    due_at: datetime | None
    remaining_seconds: int | None
    met_at: datetime | None


class TicketSlaDTO(BaseModel):
    policy_version: str
    priority: str
    cycle_number: int
    first_response: SlaTimerDTO
    resolution: SlaTimerDTO


def _settings(request: Request) -> Any:
    return request.app.state.settings


def _staff_etag(ticket: Ticket) -> str:
    return f'"{ticket.id}:{ticket.version}"'


def _project_ticket(
    ticket: Ticket, user: User, unread_count: int = 0
) -> CustomerTicket | AgentTicket:
    if user.role == Role.CUSTOMER:
        projected = CustomerTicket.model_validate(ticket)
    else:
        projected = AgentTicket.model_validate(ticket)
    projected.unread_count = unread_count
    return projected


def visible_tickets(user: User) -> Select[tuple[Ticket]]:
    """Scope at query time; ticket identifiers and email are never credentials."""
    if not user.is_active:
        raise ApiError(401, "unauthenticated", "Authentication required.")
    query = select(Ticket)
    if user.role == Role.CUSTOMER:
        return query.where(Ticket.customer_id == user.id)
    if user.role in (Role.AGENT, Role.ADMIN):
        return query
    raise ApiError(403, "forbidden", "Access denied.")


async def get_visible_ticket(db: AsyncSession, user: User, ticket_id: UUID) -> Ticket:
    ticket = await db.scalar(visible_tickets(user).where(Ticket.id == ticket_id))
    if ticket is None:
        raise ApiError(404, "not_found", "Resource not found.")
    return ticket


def _list_filters(
    user: User,
    *,
    query: str | None,
    status: TicketStatus | None,
    priority: TicketPriority | None,
    category: TicketCategory | None,
    assigned_agent_id: UUID | None,
    unassigned: bool | None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "query": query or None,
        "status": status.value if status else None,
        "priority": priority.value if priority else None,
        "category": category.value if category else None,
    }
    if user.role != Role.CUSTOMER:
        filters["assigned_agent_id"] = str(assigned_agent_id) if assigned_agent_id else None
        filters["unassigned"] = bool(unassigned) if unassigned is not None else None
    return filters


@router.get("/tickets", response_model=TicketList)
async def list_tickets(
    request: Request,
    db: Db,
    user: Annotated[User, Depends(get_current_user)],
    query: Annotated[str | None, Query(max_length=200)] = None,
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
    category: TicketCategory | None = None,
    assigned_agent_id: UUID | None = None,
    unassigned: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> TicketList:
    if user.role == Role.CUSTOMER and (assigned_agent_id is not None or unassigned is not None):
        raise ApiError(403, "forbidden", "Customer ticket lists do not support staff filters.")
    normalized_query = query.strip() if query else None
    filters = _list_filters(
        user,
        query=normalized_query,
        status=status,
        priority=priority,
        category=category,
        assigned_agent_id=assigned_agent_id,
        unassigned=unassigned,
    )
    marker = decode_cursor(_settings(request), cursor, filters)
    statement = visible_tickets(user)
    if status is not None:
        statement = statement.where(Ticket.status == status.value)
    if priority is not None:
        statement = statement.where(Ticket.priority == priority.value)
    if category is not None:
        statement = statement.where(Ticket.category == category.value)
    if normalized_query:
        pattern = f"%{normalized_query.lower()}%"
        statement = statement.where(
            or_(
                func.lower(Ticket.subject).like(pattern),
                func.lower(Ticket.description).like(pattern),
                func.lower(Ticket.customer_email_snapshot).like(pattern),
                func.cast(Ticket.ticket_sequence, String).like(pattern),
            )
        )
    if user.role != Role.CUSTOMER:
        if assigned_agent_id is not None:
            statement = statement.where(Ticket.assigned_agent_id == assigned_agent_id)
        if unassigned:
            statement = statement.where(Ticket.assigned_agent_id.is_(None))
    sort_column = Ticket.public_updated_at if user.role == Role.CUSTOMER else Ticket.updated_at
    if marker is not None:
        ts = parse_datetime(marker["ts"])
        marker_id = UUID(marker["id"])
        statement = statement.where(
            or_(sort_column < ts, and_(sort_column == ts, Ticket.id < marker_id))
        )
    rows = list(
        await db.scalars(statement.order_by(sort_column.desc(), Ticket.id.desc()).limit(limit + 1))
    )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = encode_cursor(
            _settings(request),
            {
                "filters": filters,
                "ts": (
                    last.public_updated_at if user.role == Role.CUSTOMER else last.updated_at
                ).isoformat(),
                "id": str(last.id),
            },
        )
    counts = await unread_counts(db, user, page)
    return TicketList(
        items=[_project_ticket(ticket, user, counts.get(ticket.id, 0)) for ticket in page],
        next_cursor=next_cursor,
    )


@router.post("/tickets", status_code=201, response_model=AgentTicket | CustomerTicket)
async def create_ticket(
    payload: TicketCreate,
    request: Request,
    response: Response,
    db: Db,
    user: Writer,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> CustomerTicket | AgentTicket:
    content = payload.model_dump(mode="json")
    route = "/tickets"
    record = await begin_idempotency(db, user.id, route, idempotency_key, content)
    if record is not None:
        ticket = await get_visible_ticket(db, user, record.result_id)
        response.headers["Idempotency-Replayed"] = "true"
        if user.role != Role.CUSTOMER:
            response.headers["ETag"] = _staff_etag(ticket)
        return _project_ticket(ticket, user)

    customer: User | None
    if user.role == Role.CUSTOMER:
        if payload.customer_id is not None or payload.customer_email != user.email:
            raise ApiError(403, "forbidden", "Customers can only create their own tickets.")
        customer = user
    else:
        lookup = select(User).where(User.role == Role.CUSTOMER.value, User.is_active.is_(True))
        if payload.customer_id is not None:
            lookup = lookup.where(User.id == payload.customer_id)
        else:
            lookup = lookup.where(User.email == payload.customer_email)
        customer = await db.scalar(lookup)
        if customer is None or customer.email != payload.customer_email:
            raise ApiError(
                422, "invalid_customer", "Select an active customer matching the supplied email."
            )

    # Known order references must belong to the customer. Unknown references stay
    # plain references and are never fabricated into order details.
    if payload.order_id is not None:
        owner = order_owner(payload.order_id)
        if owner is not None and owner != customer.email:
            raise ApiError(422, "invalid_order", "Order reference is not valid for this customer.")

    now = datetime.now(UTC)
    ticket = Ticket(
        customer_id=customer.id,
        customer_email_snapshot=customer.email,
        created_by_id=user.id,
        order_id=payload.order_id,
        category=payload.category.value,
        subject=payload.subject,
        description=payload.description,
        priority=payload.priority.value,
        status=TicketStatus.OPEN.value,
        created_at=now,
        updated_at=now,
        public_updated_at=now,
    )
    db.add(ticket)
    await db.flush()
    open_first_cycle(db, ticket, now)
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_id=user.id,
            event_type="ticket.created",
            field="status",
            old_value=None,
            new_value=TicketStatus.OPEN.value,
            request_id=request.state.request_id,
            visibility=Visibility.PUBLIC.value,
        )
    )
    db.add(
        OutboxEvent(ticket_id=ticket.id, kind="ticket.changed", visibility=Visibility.PUBLIC.value)
    )
    assert idempotency_key is not None
    finish_idempotency(db, user.id, route, idempotency_key, content, "ticket", ticket.id)
    await db.commit()
    if user.role != Role.CUSTOMER:
        response.headers["ETag"] = _staff_etag(ticket)
    return _project_ticket(ticket, user)


@router.get("/tickets/{ticket_id}", response_model=AgentTicket | CustomerTicket)
async def read_ticket(
    ticket_id: UUID,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CustomerTicket | AgentTicket:
    ticket = await get_visible_ticket(db, user, ticket_id)
    if user.role != Role.CUSTOMER:
        response.headers["ETag"] = _staff_etag(ticket)
    counts = await unread_counts(db, user, [ticket])
    return _project_ticket(ticket, user, counts.get(ticket.id, 0))


@router.patch("/tickets/{ticket_id}/seen", status_code=204)
async def mark_ticket_seen(
    ticket_id: UUID,
    user: Writer,
    db: Db,
) -> None:
    """Record a private unread marker for the current user. Not a read receipt."""
    ticket = await get_visible_ticket(db, user, ticket_id)
    await mark_seen(db, user, ticket.id, utcnow())
    await db.commit()


async def _has_public_staff_reply(db: AsyncSession, ticket_id: UUID) -> bool:
    return bool(
        await db.scalar(
            select(TicketMessage.id)
            .where(
                TicketMessage.ticket_id == ticket_id,
                TicketMessage.visibility == Visibility.PUBLIC.value,
                TicketMessage.sender_role_snapshot.in_([Role.AGENT.value, Role.ADMIN.value]),
            )
            .limit(1)
        )
    )


async def _validate_assignee(db: AsyncSession, assignee_id: UUID | None) -> None:
    if assignee_id is None:
        return
    user = await db.get(User, assignee_id)
    if user is None or not user.is_active or user.role not in {Role.AGENT.value, Role.ADMIN.value}:
        raise ApiError(422, "invalid_assignee", "Assignee must be an active staff user.")


@router.patch("/tickets/{ticket_id}", response_model=AgentTicket)
async def patch_ticket(
    ticket_id: UUID,
    payload: TicketPatch,
    request: Request,
    response: Response,
    db: Db,
    user: Annotated[User, Depends(require_staff)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> AgentTicket:
    ticket = await db.scalar(
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if ticket is None:
        raise ApiError(404, "not_found", "Resource not found.")
    if if_match is None:
        raise ApiError(428, "precondition_required", "If-Match is required.")
    if if_match != _staff_etag(ticket):
        raise ApiError(412, "precondition_failed", "Ticket changed. Refresh before retrying.")
    await _validate_assignee(
        db,
        payload.assigned_agent_id
        if "assigned_agent_id" in payload.model_fields_set
        else ticket.assigned_agent_id,
    )

    changes: list[tuple[str, str | None, str | None]] = []
    new_status = payload.status.value if payload.status is not None else ticket.status
    if new_status != ticket.status:
        validate_status_transition(ticket.status, new_status, payload.reason)
        if new_status == TicketStatus.RESOLVED.value and not await _has_public_staff_reply(
            db, ticket.id
        ):
            raise ApiError(
                409,
                "resolution_requires_public_reply",
                "Resolve after a public staff reply explains the outcome.",
            )
        changes.append(("status", ticket.status, new_status))
    if payload.priority is not None and payload.priority.value != ticket.priority:
        changes.append(("priority", ticket.priority, payload.priority.value))
    if "assigned_agent_id" in payload.model_fields_set:
        new_assignee = str(payload.assigned_agent_id) if payload.assigned_agent_id else None
        old_assignee = str(ticket.assigned_agent_id) if ticket.assigned_agent_id else None
        if new_assignee != old_assignee:
            changes.append(("assigned_agent_id", old_assignee, new_assignee))
    if not changes:
        response.headers["ETag"] = _staff_etag(ticket)
        return AgentTicket.model_validate(ticket)

    now = datetime.now(UTC)
    public_change = any(field in {"status", "priority"} for field, _, _ in changes)
    for field, old, new in changes:
        setattr(ticket, field, new if field != "assigned_agent_id" or new is None else UUID(new))
        if field == "status":
            if new == TicketStatus.RESOLVED.value:
                ticket.resolved_at = now
                ticket.closed_at = None
            elif new == TicketStatus.CLOSED.value:
                ticket.closed_at = now
            elif old in {TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value}:
                ticket.resolved_at = None
                ticket.closed_at = None
            assert old is not None and new is not None
            await apply_status_change(db, ticket, old, new, now)
        elif field == "priority":
            assert new is not None
            await apply_priority_change(db, ticket, new, now)
        db.add(
            TicketEvent(
                ticket_id=ticket.id,
                actor_id=user.id,
                event_type=f"{field}.changed",
                field=field,
                old_value=old,
                new_value=new,
                reason=payload.reason if field == "status" else None,
                request_id=request.state.request_id,
                visibility=Visibility.PUBLIC.value
                if field in {"status", "priority"}
                else Visibility.INTERNAL.value,
            )
        )
    ticket.version += 1
    ticket.updated_at = now
    if public_change:
        ticket.public_updated_at = now
    db.add(
        OutboxEvent(
            ticket_id=ticket.id,
            kind="ticket.changed",
            visibility=Visibility.PUBLIC.value if public_change else Visibility.INTERNAL.value,
        )
    )
    await db.commit()
    response.headers["ETag"] = _staff_etag(ticket)
    return AgentTicket.model_validate(ticket)


@router.get("/tickets/{ticket_id}/events", response_model=TicketEventPage)
async def ticket_events(
    ticket_id: UUID,
    request: Request,
    db: Db,
    _user: Annotated[User, Depends(require_staff)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> TicketEventPage:
    if await db.get(Ticket, ticket_id) is None:
        raise ApiError(404, "not_found", "Resource not found.")
    filters = {"ticket_id": str(ticket_id), "kind": "events"}
    marker = decode_cursor(_settings(request), cursor, filters)
    statement = select(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
    if marker:
        ts = parse_datetime(marker["ts"])
        marker_id = UUID(marker["id"])
        statement = statement.where(
            or_(
                TicketEvent.created_at < ts,
                and_(TicketEvent.created_at == ts, TicketEvent.id < marker_id),
            )
        )
    rows = list(
        await db.scalars(
            statement.order_by(TicketEvent.created_at.desc(), TicketEvent.id.desc()).limit(
                limit + 1
            )
        )
    )
    page = rows[:limit]
    next_cursor = (
        encode_cursor(
            _settings(request),
            {"filters": filters, "ts": page[-1].created_at.isoformat(), "id": str(page[-1].id)},
        )
        if len(rows) > limit and page
        else None
    )
    return TicketEventPage(
        items=[TicketEventDTO.model_validate(row) for row in page], next_cursor=next_cursor
    )


@router.get("/tickets/{ticket_id}/public-history", response_model=PublicHistoryPage)
async def public_history(
    ticket_id: UUID,
    request: Request,
    db: Db,
    user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> PublicHistoryPage:
    await get_visible_ticket(db, user, ticket_id)
    filters = {"ticket_id": str(ticket_id), "kind": "public-history"}
    marker = decode_cursor(_settings(request), cursor, filters)
    statement = select(TicketEvent).where(
        TicketEvent.ticket_id == ticket_id,
        TicketEvent.visibility == Visibility.PUBLIC.value,
        TicketEvent.field == "status",
    )
    if marker:
        ts = parse_datetime(marker["ts"])
        marker_id = UUID(marker["id"])
        statement = statement.where(
            or_(
                TicketEvent.created_at < ts,
                and_(TicketEvent.created_at == ts, TicketEvent.id < marker_id),
            )
        )
    rows = list(
        await db.scalars(
            statement.order_by(TicketEvent.created_at.desc(), TicketEvent.id.desc()).limit(
                limit + 1
            )
        )
    )
    page = rows[:limit]
    next_cursor = (
        encode_cursor(
            _settings(request),
            {"filters": filters, "ts": page[-1].created_at.isoformat(), "id": str(page[-1].id)},
        )
        if len(rows) > limit and page
        else None
    )
    return PublicHistoryPage(
        items=[
            PublicHistoryItem(
                id=row.id,
                event_type=row.event_type,
                field=row.field,
                old_value=row.old_value,
                new_value=row.new_value,
                created_at=row.created_at,
            )
            for row in page
        ],
        next_cursor=next_cursor,
    )


@router.get("/tickets/{ticket_id}/sla", response_model=TicketSlaDTO)
async def ticket_sla(
    ticket_id: UUID,
    db: Db,
    _user: Annotated[User, Depends(require_staff)],
) -> TicketSlaDTO:
    if await db.get(Ticket, ticket_id) is None:
        raise ApiError(404, "not_found", "Resource not found.")
    cycle = await active_cycle(db, ticket_id)
    if cycle is None:
        raise ApiError(404, "sla_unavailable", "No active SLA cycle for this ticket.")
    return TicketSlaDTO(**sla_projection(cycle, utcnow()))


@router.get("/agents", response_model=AgentList)
async def agents(
    _user: Annotated[User, Depends(require_staff)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentList:
    rows = await db.scalars(
        select(User)
        .where(User.role.in_([Role.AGENT.value, Role.ADMIN.value]), User.is_active.is_(True))
        .order_by(User.display_name, User.id)
    )
    return AgentList(items=[AgentSummary.model_validate(user) for user in rows])


@router.get("/customers", response_model=CustomerList)
async def customers(
    request: Request,
    db: Db,
    _user: Annotated[User, Depends(require_staff)],
    query: Annotated[str, Query(min_length=2, max_length=254)],
    limit: Annotated[int, Query(ge=1, le=20)] = 20,
    cursor: str | None = None,
) -> CustomerList:
    q = query.strip().lower()
    await hit_throttle(db, purpose="customers:lookup:ip", value=client_ip(request), limit=120)
    filters = {"query": q}
    marker = decode_cursor(_settings(request), cursor, filters)
    statement = select(User).where(User.role == Role.CUSTOMER.value, User.is_active.is_(True))
    pattern = f"%{q}%"
    statement = statement.where(
        or_(func.lower(User.email).like(pattern), func.lower(User.display_name).like(pattern))
    )
    if marker:
        statement = statement.where(
            or_(
                User.email > marker["email"],
                and_(User.email == marker["email"], User.id > UUID(marker["id"])),
            )
        )
    rows = list(
        await db.scalars(statement.order_by(User.email.asc(), User.id.asc()).limit(limit + 1))
    )
    page = rows[:limit]
    next_cursor = (
        encode_cursor(
            _settings(request),
            {"filters": filters, "email": page[-1].email, "id": str(page[-1].id)},
        )
        if len(rows) > limit and page
        else None
    )
    return CustomerList(
        items=[CustomerSummary.model_validate(row) for row in page], next_cursor=next_cursor
    )
