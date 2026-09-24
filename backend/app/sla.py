"""SLA policy, cycle transitions, and deadline projection.

Project defaults (PLAN.md §11.4), not business promises. A 24/7 clock is used:
business-hours/holiday calendars are out of scope. All arithmetic is UTC-duration
based, so it is independent of the viewer's timezone/DST.

- First-response timer starts at cycle creation and stops only on the first PUBLIC
  agent reply; internal notes never satisfy it, and it never pauses.
- Resolution timer runs while OPEN/IN_PROGRESS/WAITING_FOR_PROVIDER; WAITING_FOR_CUSTOMER
  pauses it. RESOLVED/CLOSED complete the cycle; reopening starts a new cycle.
- Priority changes accrue eligible time; they never reset the clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OutboxEvent, Ticket, TicketEvent, TicketSlaCycle, Visibility

POLICY_VERSION = "2026-01-project-defaults"
DUE_SOON_FRACTION = 0.2

FIRST_RESPONSE_TARGETS: dict[str, int] = {
    "URGENT": 15 * 60,
    "HIGH": 60 * 60,
    "MEDIUM": 4 * 60 * 60,
    "LOW": 8 * 60 * 60,
}
RESOLUTION_TARGETS: dict[str, int] = {
    "URGENT": 4 * 60 * 60,
    "HIGH": 8 * 60 * 60,
    "MEDIUM": 24 * 60 * 60,
    "LOW": 72 * 60 * 60,
}

ACTIVE_RESOLUTION_STATUSES = {"OPEN", "IN_PROGRESS", "WAITING_FOR_PROVIDER"}
PAUSED_RESOLUTION_STATUSES = {"WAITING_FOR_CUSTOMER"}
COMPLETED_STATUSES = {"RESOLVED", "CLOSED"}


@dataclass(frozen=True)
class Timer:
    state: str  # met | on_track | due_soon | overdue
    target_seconds: int
    due_at: datetime | None
    remaining_seconds: int | None
    met_at: datetime | None


def first_response_target(priority: str) -> int:
    return FIRST_RESPONSE_TARGETS.get(priority, FIRST_RESPONSE_TARGETS["MEDIUM"])


def resolution_target(priority: str) -> int:
    return RESOLUTION_TARGETS.get(priority, RESOLUTION_TARGETS["MEDIUM"])


def _classify(due_at: datetime, target_seconds: int, now: datetime) -> Timer:
    remaining = (due_at - now).total_seconds()
    if remaining <= 0:
        state = "overdue"
    elif remaining <= DUE_SOON_FRACTION * target_seconds:
        state = "due_soon"
    else:
        state = "on_track"
    return Timer(
        state=state,
        target_seconds=target_seconds,
        due_at=due_at,
        remaining_seconds=max(0, int(remaining)),
        met_at=None,
    )


def effective_resolution_due(cycle: TicketSlaCycle, now: datetime) -> datetime:
    due = cycle.resolution_due_at
    if cycle.resolution_pause_started_at is not None:
        due = due + (now - cycle.resolution_pause_started_at)
    return due


def first_response_timer(cycle: TicketSlaCycle, now: datetime) -> Timer:
    target = first_response_target(cycle.priority)
    if cycle.first_response_at is not None:
        return Timer("met", target, cycle.first_response_due_at, 0, cycle.first_response_at)
    return _classify(cycle.first_response_due_at, target, now)


def resolution_timer(cycle: TicketSlaCycle, now: datetime) -> Timer:
    target = resolution_target(cycle.priority)
    if cycle.resolution_completed_at is not None:
        due = effective_resolution_due(cycle, now)
        state = "overdue" if cycle.resolution_completed_at > due else "met"
        return Timer(state, target, due, 0, cycle.resolution_completed_at)
    if cycle.resolution_pause_started_at is not None:
        return Timer("paused", target, effective_resolution_due(cycle, now), None, None)
    return _classify(cycle.resolution_due_at, target, now)


def eligible_resolution_seconds(cycle: TicketSlaCycle, now: datetime) -> float:
    elapsed = (now - cycle.resolution_started_at).total_seconds()
    paused = float(cycle.resolution_paused_seconds)
    if cycle.resolution_pause_started_at is not None:
        paused += (now - cycle.resolution_pause_started_at).total_seconds()
    return max(0.0, elapsed - paused)


# --- mutations ---------------------------------------------------------------


def _new_cycle(ticket: Ticket, now: datetime, cycle_number: int) -> TicketSlaCycle:
    priority = ticket.priority
    return TicketSlaCycle(
        ticket_id=ticket.id,
        cycle_number=cycle_number,
        policy_version=POLICY_VERSION,
        priority=priority,
        first_response_due_at=now + timedelta(seconds=first_response_target(priority)),
        resolution_started_at=now,
        resolution_due_at=now + timedelta(seconds=resolution_target(priority)),
        is_active=True,
        created_at=now,
    )


def open_first_cycle(db: AsyncSession, ticket: Ticket, now: datetime) -> TicketSlaCycle:
    cycle = _new_cycle(ticket, now, 1)
    db.add(cycle)
    return cycle


async def active_cycle(db: AsyncSession, ticket_id: UUID) -> TicketSlaCycle | None:
    return cast(
        "TicketSlaCycle | None",
        await db.scalar(
            select(TicketSlaCycle)
            .where(TicketSlaCycle.ticket_id == ticket_id, TicketSlaCycle.is_active.is_(True))
            .order_by(TicketSlaCycle.cycle_number.desc())
            .limit(1)
        ),
    )


async def record_first_public_response(
    db: AsyncSession, ticket: Ticket, now: datetime
) -> TicketSlaCycle | None:
    cycle = await active_cycle(db, ticket.id)
    if cycle is None or cycle.first_response_at is not None:
        return cycle
    cycle.first_response_at = now
    if now > cycle.first_response_due_at:
        cycle.first_response_breached_at = cycle.first_response_due_at
    return cycle


async def apply_priority_change(
    db: AsyncSession, ticket: Ticket, new_priority: str, now: datetime
) -> None:
    cycle = await active_cycle(db, ticket.id)
    if cycle is None:
        return
    elapsed = eligible_resolution_seconds(cycle, now)
    cycle.priority = new_priority
    cycle.resolution_due_at = now + timedelta(seconds=resolution_target(new_priority) - elapsed)
    if cycle.first_response_at is None:
        cycle.first_response_due_at = cycle.resolution_started_at + timedelta(
            seconds=first_response_target(new_priority)
        )


async def apply_status_change(
    db: AsyncSession, ticket: Ticket, old_status: str, new_status: str, now: datetime
) -> None:
    """Keep the active cycle consistent with a committed status transition."""
    if old_status == new_status:
        return

    if new_status in COMPLETED_STATUSES:
        cycle = await active_cycle(db, ticket.id)
        if cycle is not None:
            due = effective_resolution_due(cycle, now)
            cycle.resolution_completed_at = now
            cycle.is_active = False
            cycle.resolution_pause_started_at = None
            if now > due:
                cycle.resolution_breached_at = due
        return

    if new_status == "WAITING_FOR_CUSTOMER":
        cycle = await active_cycle(db, ticket.id)
        if cycle is not None and cycle.resolution_pause_started_at is None:
            cycle.resolution_pause_started_at = now
        return

    # A running state (OPEN/IN_PROGRESS/WAITING_FOR_PROVIDER).
    cycle = await active_cycle(db, ticket.id)
    if cycle is not None and cycle.resolution_pause_started_at is not None:
        paused = now - cycle.resolution_pause_started_at
        cycle.resolution_paused_seconds += int(paused.total_seconds())
        cycle.resolution_due_at = cycle.resolution_due_at + paused
        cycle.resolution_pause_started_at = None

    if old_status in COMPLETED_STATUSES:
        # Reopen: the historical first-response result stays on the old cycle, and a
        # new resolution cycle starts now with the current priority.
        last = await db.scalar(
            select(TicketSlaCycle.cycle_number)
            .where(TicketSlaCycle.ticket_id == ticket.id)
            .order_by(TicketSlaCycle.cycle_number.desc())
            .limit(1)
        )
        new_cycle = _new_cycle(ticket, now, int(last or 0) + 1)
        new_cycle.first_response_at = ticket.first_agent_response_at or now
        new_cycle.first_response_due_at = new_cycle.first_response_at
        db.add(new_cycle)


async def record_breaches(
    db: AsyncSession, now: datetime, limit: int = 100
) -> list[tuple[UUID, str]]:
    """Persist first/resolution breaches once each and emit a staff-only audit event.

    Idempotent: a breach timestamp is only written once, so repeated worker runs add
    no events. Returns the (ticket_id, field) pairs newly marked.
    """
    rows = list(
        await db.scalars(
            select(TicketSlaCycle)
            .where(
                TicketSlaCycle.is_active.is_(True),
                or_(
                    and_(
                        TicketSlaCycle.first_response_at.is_(None),
                        TicketSlaCycle.first_response_breached_at.is_(None),
                        TicketSlaCycle.first_response_due_at < now,
                    ),
                    and_(
                        TicketSlaCycle.resolution_completed_at.is_(None),
                        TicketSlaCycle.resolution_pause_started_at.is_(None),
                        TicketSlaCycle.resolution_breached_at.is_(None),
                        TicketSlaCycle.resolution_due_at < now,
                    ),
                ),
            )
            .order_by(TicketSlaCycle.resolution_due_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    breached: list[tuple[UUID, str]] = []
    for cycle in rows:
        if (
            cycle.first_response_at is None
            and cycle.first_response_breached_at is None
            and cycle.first_response_due_at < now
        ):
            cycle.first_response_breached_at = cycle.first_response_due_at
            db.add(
                TicketEvent(
                    ticket_id=cycle.ticket_id,
                    event_type="sla.breached",
                    field="first_response",
                    old_value=None,
                    new_value=cycle.first_response_due_at.isoformat(),
                    visibility=Visibility.INTERNAL.value,
                    created_at=now,
                )
            )
            breached.append((cycle.ticket_id, "first_response"))
        if (
            cycle.resolution_completed_at is None
            and cycle.resolution_pause_started_at is None
            and cycle.resolution_breached_at is None
            and cycle.resolution_due_at < now
        ):
            cycle.resolution_breached_at = cycle.resolution_due_at
            db.add(
                TicketEvent(
                    ticket_id=cycle.ticket_id,
                    event_type="sla.breached",
                    field="resolution",
                    old_value=None,
                    new_value=cycle.resolution_due_at.isoformat(),
                    visibility=Visibility.INTERNAL.value,
                    created_at=now,
                )
            )
            breached.append((cycle.ticket_id, "resolution"))
    if breached:
        # Staff-only invalidation; customers are never notified about internal SLA.
        for ticket_id, _field in breached:
            db.add(
                OutboxEvent(
                    ticket_id=ticket_id,
                    kind="ticket.changed",
                    visibility=Visibility.INTERNAL.value,
                )
            )
    return breached


def sla_projection(cycle: TicketSlaCycle, now: datetime) -> dict[str, Any]:
    first = first_response_timer(cycle, now)
    resolution = resolution_timer(cycle, now)

    def timer(t: Timer) -> dict[str, Any]:
        return {
            "state": t.state,
            "target_seconds": t.target_seconds,
            "due_at": t.due_at,
            "remaining_seconds": t.remaining_seconds,
            "met_at": t.met_at,
        }

    return {
        "policy_version": cycle.policy_version,
        "priority": cycle.priority,
        "cycle_number": cycle.cycle_number,
        "first_response": timer(first),
        "resolution": timer(resolution),
    }


def utcnow() -> datetime:
    return datetime.now(UTC)
