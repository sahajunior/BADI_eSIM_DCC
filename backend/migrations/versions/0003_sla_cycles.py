"""add ticket SLA cycles with policy-target backfill

Revision ID: 0003_sla_cycles
Revises: 0002_conversations
Create Date: 2026-09-22 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_sla_cycles"
down_revision: str | None = "0002_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRIORITY_VALUES = ("LOW", "MEDIUM", "HIGH", "URGENT")
POLICY_VERSION = "2026-01-project-defaults"

_FIRST_RESPONSE_MINUTES = {"URGENT": 15, "HIGH": 60, "MEDIUM": 240, "LOW": 480}
_RESOLUTION_MINUTES = {"URGENT": 240, "HIGH": 480, "MEDIUM": 1440, "LOW": 4320}


def _intervals(table: str, minutes: dict[str, int]) -> str:
    branches = " ".join(
        f"WHEN {priority!r} THEN INTERVAL '{value} minutes'" for priority, value in minutes.items()
    )
    return f"CASE {table}.priority {branches} ELSE INTERVAL '240 minutes' END"


def upgrade() -> None:
    op.create_table(
        "ticket_sla_cycles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cycle_number", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(length=40), nullable=False),
        sa.Column("priority", sa.String(length=20), nullable=False),
        sa.Column("first_response_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_response_breached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "resolution_paused_seconds", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("resolution_pause_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_breached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "cycle_number >= 1", name=op.f("ck_ticket_sla_cycles_cycle_number_positive")
        ),
        sa.CheckConstraint(
            "resolution_paused_seconds >= 0",
            name=op.f("ck_ticket_sla_cycles_paused_seconds_non_negative"),
        ),
        sa.CheckConstraint(
            "priority IN ('LOW', 'MEDIUM', 'HIGH', 'URGENT')",
            name=op.f("ck_ticket_sla_cycles_priority_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name="fk_ticket_sla_cycles_ticket_id_tickets"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_sla_cycles")),
        sa.UniqueConstraint("ticket_id", "cycle_number", name="uq_ticket_sla_cycles_ticket_cycle"),
    )
    op.create_index(
        "ix_ticket_sla_cycles_active_due",
        "ticket_sla_cycles",
        ["is_active", "resolution_due_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_ticket_sla_cycles_ticket",
        "ticket_sla_cycles",
        ["ticket_id", "cycle_number"],
        unique=False,
    )

    # Backfill one cycle per existing ticket using the project-default policy.
    op.execute(
        f"""
        INSERT INTO ticket_sla_cycles (
            ticket_id, cycle_number, policy_version, priority,
            first_response_due_at, first_response_at, first_response_breached_at,
            resolution_started_at, resolution_due_at,
            resolution_completed_at, resolution_breached_at, is_active
        )
        SELECT
            t.id,
            1,
            '{POLICY_VERSION}',
            t.priority,
            t.created_at + {_intervals("t", _FIRST_RESPONSE_MINUTES)},
            t.first_agent_response_at,
            CASE
                WHEN t.first_agent_response_at IS NOT NULL
                 AND t.first_agent_response_at
                     > t.created_at + {_intervals("t", _FIRST_RESPONSE_MINUTES)}
                THEN t.created_at + {_intervals("t", _FIRST_RESPONSE_MINUTES)}
                ELSE NULL
            END,
            t.created_at,
            t.created_at + {_intervals("t", _RESOLUTION_MINUTES)},
            CASE WHEN t.status IN ('RESOLVED', 'CLOSED')
                 THEN COALESCE(t.resolved_at, t.closed_at, t.updated_at) ELSE NULL END,
            CASE
                WHEN t.status IN ('RESOLVED', 'CLOSED')
                 AND COALESCE(t.resolved_at, t.closed_at, t.updated_at)
                     > t.created_at + {_intervals("t", _RESOLUTION_MINUTES)}
                THEN t.created_at + {_intervals("t", _RESOLUTION_MINUTES)}
                ELSE NULL
            END,
            (t.status NOT IN ('RESOLVED', 'CLOSED'))
        FROM tickets AS t
        """
    )


def downgrade() -> None:
    op.drop_index("ix_ticket_sla_cycles_ticket", table_name="ticket_sla_cycles")
    op.drop_index("ix_ticket_sla_cycles_active_due", table_name="ticket_sla_cycles")
    op.drop_table("ticket_sla_cycles")
