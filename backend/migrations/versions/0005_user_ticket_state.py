"""add private per-user ticket seen markers

Revision ID: 0005_user_ticket_state
Revises: 0004_saved_replies
Create Date: 2026-09-22 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_user_ticket_state"
down_revision: str | None = "0004_saved_replies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_ticket_state",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_ticket_state_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name="fk_user_ticket_state_ticket_id_tickets"
        ),
        sa.PrimaryKeyConstraint("user_id", "ticket_id", name=op.f("pk_user_ticket_state")),
    )
    op.create_index("ix_user_ticket_state_ticket", "user_ticket_state", ["ticket_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_user_ticket_state_ticket", table_name="user_ticket_state")
    op.drop_table("user_ticket_state")
