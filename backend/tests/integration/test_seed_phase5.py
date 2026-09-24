"""The demo seed must be deterministic, idempotent, and cover the documented fixture set."""

from __future__ import annotations

import pytest
from conftest import TEST_PASSWORD, session_for
from sqlalchemy import func, select

from app.models import (
    TICKET_CATEGORY_VALUES,
    TICKET_PRIORITY_VALUES,
    TICKET_STATUS_VALUES,
    Ticket,
    TicketEvent,
    TicketMessage,
    User,
    Visibility,
)
from app.seed import DEMO_TICKETS, seed_demo


@pytest.mark.asyncio
async def test_demo_seed_covers_the_fixture_matrix(clean_database: str) -> None:
    async for session in session_for(clean_database):
        assert await seed_demo(session, TEST_PASSWORD) == (5, len(DEMO_TICKETS))
        await session.commit()

        statuses = set(await session.scalars(select(Ticket.status).distinct()))
        priorities = set(await session.scalars(select(Ticket.priority).distinct()))
        categories = set(await session.scalars(select(Ticket.category).distinct()))
        unassigned = await session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.assigned_agent_id.is_(None))
        )
        assigned = await session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.assigned_agent_id.is_not(None))
        )
        messages = await session.scalar(select(func.count()).select_from(TicketMessage))
        internal = await session.scalar(
            select(func.count())
            .select_from(TicketMessage)
            .where(TicketMessage.visibility == Visibility.INTERNAL.value)
        )
        events = await session.scalar(select(func.count()).select_from(TicketEvent))
        staff_events = await session.scalar(
            select(func.count())
            .select_from(TicketEvent)
            .where(TicketEvent.visibility == Visibility.INTERNAL.value)
        )
        customers = await session.scalar(
            select(func.count()).select_from(User).where(User.role == "CUSTOMER")
        )
        agents = await session.scalar(
            select(func.count()).select_from(User).where(User.role == "AGENT")
        )
        admins = await session.scalar(
            select(func.count()).select_from(User).where(User.role == "ADMIN")
        )

        assert statuses == set(TICKET_STATUS_VALUES)
        assert priorities == set(TICKET_PRIORITY_VALUES)
        assert categories == set(TICKET_CATEGORY_VALUES)
        assert unassigned == 1
        assert assigned and assigned >= 1
        assert messages and messages >= 6
        assert internal and internal >= 2
        assert events and events > messages
        assert staff_events and staff_events >= 1
        assert (customers, agents, admins) == (2, 2, 1)

        # Re-running is a no-op and does not duplicate messages/events.
        assert await seed_demo(session, "A different demo password!") == (0, 0)
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(TicketMessage)) == messages
        assert await session.scalar(select(func.count()).select_from(TicketEvent)) == events
