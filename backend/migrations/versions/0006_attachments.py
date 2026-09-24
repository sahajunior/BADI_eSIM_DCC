"""add private attachments with scan state and expiry

Revision ID: 0006_attachments
Revises: 0005_user_ticket_state
Create Date: 2026-09-22 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_attachments"
down_revision: str | None = "0005_user_ticket_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("uploader_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("detected_type", sa.String(length=30), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("scan_state", sa.String(length=20), server_default="PENDING", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("attached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scan_state IN ('PENDING', 'CLEAN', 'REJECTED')",
            name=op.f("ck_attachments_scan_state_allowed"),
        ),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_attachments_size_non_negative")),
        sa.CheckConstraint(
            "char_length(original_name) BETWEEN 1 AND 255",
            name=op.f("ck_attachments_original_name_length"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name="fk_attachments_ticket_id_tickets"
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["ticket_messages.id"],
            name="fk_attachments_message_id_ticket_messages",
        ),
        sa.ForeignKeyConstraint(
            ["uploader_id"], ["users.id"], name="fk_attachments_uploader_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attachments")),
        sa.UniqueConstraint("storage_key", name="uq_attachments_storage_key"),
    )
    op.create_index("ix_attachments_message", "attachments", ["message_id"], unique=False)
    op.create_index("ix_attachments_ticket", "attachments", ["ticket_id"], unique=False)
    op.create_index(
        "ix_attachments_expiry", "attachments", ["scan_state", "expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_attachments_expiry", table_name="attachments")
    op.drop_index("ix_attachments_ticket", table_name="attachments")
    op.drop_index("ix_attachments_message", table_name="attachments")
    op.drop_table("attachments")
