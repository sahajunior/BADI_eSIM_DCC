"""Small operational dashboard projection (staff/admin).

Explicit definitions:
- open: status in OPEN/IN_PROGRESS/WAITING_FOR_CUSTOMER/WAITING_FOR_PROVIDER
- overdue: an active SLA cycle whose first-response or resolution deadline has passed
  and has not been met/completed (paused resolution is not overdue)
- first_response/resolution: met vs breached vs pending across cycles
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.auth.dependencies import get_db, require_staff
from app.models import Ticket, TicketSlaCycle, User
from app.sla import POLICY_VERSION, utcnow

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

OPEN_STATUSES = ("OPEN", "IN_PROGRESS", "WAITING_FOR_CUSTOMER", "WAITING_FOR_PROVIDER")


class GroupCount(BaseModel):
    key: str
    count: int


class TimerCounts(BaseModel):
    met: int
    breached: int
    pending: int


class DashboardSummary(BaseModel):
    policy_version: str
    total_tickets: int
    open_tickets: int
    unassigned: int
    overdue: int
    by_status: list[GroupCount]
    by_priority: list[GroupCount]
    first_response: TimerCounts
    resolution: TimerCounts


async def _grouped(db: AsyncSession, statement: Select[Any]) -> list[GroupCount]:
    rows = await db.execute(statement)
    return [GroupCount(key=str(key), count=int(count)) for key, count in rows.all()]


@router.get("/dashboard/summary", response_model=DashboardSummary)
async def dashboard_summary(
    _user: Annotated[User, Depends(require_staff)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DashboardSummary:
    now: datetime = utcnow()

    total = int(await db.scalar(select(func.count()).select_from(Ticket)) or 0)
    open_tickets = int(
        await db.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.status.in_(OPEN_STATUSES))
        )
        or 0
    )
    unassigned = int(
        await db.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.status.in_(OPEN_STATUSES), Ticket.assigned_agent_id.is_(None))
        )
        or 0
    )
    by_status = await _grouped(db, select(Ticket.status, func.count()).group_by(Ticket.status))
    by_priority = await _grouped(
        db,
        select(Ticket.priority, func.count())
        .where(Ticket.status.in_(OPEN_STATUSES))
        .group_by(Ticket.priority),
    )
    overdue = int(
        await db.scalar(
            select(func.count())
            .select_from(TicketSlaCycle)
            .where(
                TicketSlaCycle.is_active.is_(True),
                (
                    (TicketSlaCycle.first_response_at.is_(None))
                    & (TicketSlaCycle.first_response_due_at < now)
                )
                | (
                    (TicketSlaCycle.resolution_completed_at.is_(None))
                    & (TicketSlaCycle.resolution_pause_started_at.is_(None))
                    & (TicketSlaCycle.resolution_due_at < now)
                ),
            )
        )
        or 0
    )

    first_met = case((TicketSlaCycle.first_response_at.is_not(None), 1), else_=0)
    first_breached = case((TicketSlaCycle.first_response_breached_at.is_not(None), 1), else_=0)
    first_pending = case((TicketSlaCycle.first_response_at.is_(None), 1), else_=0)
    res_met = case(
        (
            (TicketSlaCycle.resolution_completed_at.is_not(None))
            & (TicketSlaCycle.resolution_breached_at.is_(None)),
            1,
        ),
        else_=0,
    )
    res_breached = case((TicketSlaCycle.resolution_breached_at.is_not(None), 1), else_=0)
    res_pending = case((TicketSlaCycle.resolution_completed_at.is_(None), 1), else_=0)
    row = (
        await db.execute(
            select(
                func.sum(first_met),
                func.sum(first_breached),
                func.sum(first_pending),
                func.sum(res_met),
                func.sum(res_breached),
                func.sum(res_pending),
            ).select_from(TicketSlaCycle)
        )
    ).one()

    return DashboardSummary(
        policy_version=POLICY_VERSION,
        total_tickets=total,
        open_tickets=open_tickets,
        unassigned=unassigned,
        overdue=overdue,
        by_status=by_status,
        by_priority=by_priority,
        first_response=TimerCounts(
            met=int(row[0] or 0), breached=int(row[1] or 0), pending=int(row[2] or 0)
        ),
        resolution=TimerCounts(
            met=int(row[3] or 0), breached=int(row[4] or 0), pending=int(row[5] or 0)
        ),
    )
