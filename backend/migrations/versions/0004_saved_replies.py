"""add agent saved replies (macros)

Revision ID: 0004_saved_replies
Revises: 0003_sla_cycles
Create Date: 2026-09-22 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_saved_replies"
down_revision: str | None = "0003_sla_cycles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORY_VALUES = (
    "INSTALLATION",
    "ACTIVATION",
    "CONNECTIVITY",
    "ORDER",
    "TOPUP",
    "REFUND",
    "OTHER",
)


def upgrade() -> None:
    op.create_table(
        "saved_replies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 120", name=op.f("ck_saved_replies_title_length")
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 10000", name=op.f("ck_saved_replies_body_length")
        ),
        sa.CheckConstraint(
            "category IS NULL OR category IN "
            "('INSTALLATION', 'ACTIVATION', 'CONNECTIVITY', 'ORDER', 'TOPUP', 'REFUND', 'OTHER')",
            name=op.f("ck_saved_replies_category_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name="fk_saved_replies_author_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_replies")),
    )
    op.create_index(
        "ix_saved_replies_active_title",
        "saved_replies",
        ["is_active", "title"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_saved_replies_active_title", table_name="saved_replies")
    op.drop_table("saved_replies")
