"""add reply notification deliveries and preferences

Revision ID: 0007_notifications
Revises: 0006_attachments
Create Date: 2026-09-22 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_notifications"
down_revision: str | None = "0006_attachments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_preferences",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email_on_reply", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_notification_preferences_user_id_users"
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_notification_preferences")),
    )

    op.create_table(
        "notification_deliveries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="PENDING", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("provider_reference", sa.String(length=200), nullable=True),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED', 'SKIPPED')",
            name=op.f("ck_notification_deliveries_status_allowed"),
        ),
        sa.CheckConstraint(
            "channel IN ('email', 'queue')",
            name=op.f("ck_notification_deliveries_channel_allowed"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_notification_deliveries_attempts_non_negative")
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 200",
            name=op.f("ck_notification_deliveries_last_error_length"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name="fk_notification_deliveries_ticket_id_tickets"
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["ticket_messages.id"],
            name="fk_notification_deliveries_message_id_ticket_messages",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"], ["users.id"], name="fk_notification_deliveries_recipient_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
        sa.UniqueConstraint(
            "message_id", "recipient_id", "channel", name="uq_notification_deliveries_recipient"
        ),
    )
    op.create_index(
        "uq_notification_deliveries_queue",
        "notification_deliveries",
        ["message_id", "channel"],
        unique=True,
        postgresql_where=sa.text("recipient_id IS NULL"),
    )
    op.create_index(
        "ix_notification_deliveries_pending",
        "notification_deliveries",
        ["status", "available_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_deliveries_pending", table_name="notification_deliveries")
    op.drop_index(
        "uq_notification_deliveries_queue",
        table_name="notification_deliveries",
        postgresql_where=sa.text("recipient_id IS NULL"),
    )
    op.drop_table("notification_deliveries")
    op.drop_table("notification_preferences")
