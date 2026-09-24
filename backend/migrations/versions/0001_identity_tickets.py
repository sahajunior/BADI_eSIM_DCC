"""identity and ticket tables

Revision ID: 0001_identity_tickets
Revises:
Create Date: 2026-09-21 00:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_identity_tickets"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_VALUES = ("CUSTOMER", "AGENT", "ADMIN")
CATEGORY_VALUES = (
    "INSTALLATION",
    "ACTIVATION",
    "CONNECTIVITY",
    "ORDER",
    "TOPUP",
    "REFUND",
    "OTHER",
)
PRIORITY_VALUES = ("LOW", "MEDIUM", "HIGH", "URGENT")
STATUS_VALUES = (
    "OPEN",
    "IN_PROGRESS",
    "WAITING_FOR_CUSTOMER",
    "WAITING_FOR_PROVIDER",
    "RESOLVED",
    "CLOSED",
)


def _in_values(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS "pgcrypto"'))
    op.execute(sa.schema.CreateSequence(sa.Sequence("ticket_sequence_seq", start=1001)))

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(_in_values("role", ROLE_VALUES), name=op.f("ck_users_role_allowed")),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lowercase")),
        sa.CheckConstraint(
            "char_length(email) BETWEEN 3 AND 254", name=op.f("ck_users_email_length")
        ),
        sa.CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 100",
            name=op.f("ck_users_display_name_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "auth_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "char_length(token_hash) = 64", name=op.f("ck_auth_sessions_token_hash_length")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_auth_sessions_user_id_users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
    )
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_index(
        "ix_auth_sessions_user_id_expires_at", "auth_sessions", ["user_id", "expires_at"]
    )

    op.create_table(
        "auth_throttles",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "char_length(key_hash) = 64", name=op.f("ck_auth_throttles_key_hash_length")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_auth_throttles_attempts_non_negative")),
        sa.PrimaryKeyConstraint("key_hash", name=op.f("pk_auth_throttles")),
    )

    op.create_table(
        "tickets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "ticket_sequence",
            sa.Integer(),
            server_default=sa.text("nextval('ticket_sequence_seq')"),
            nullable=False,
        ),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_email_snapshot", sa.String(length=254), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(length=20), server_default="MEDIUM", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="OPEN", nullable=False),
        sa.Column("assigned_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "public_updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("first_agent_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            _in_values("category", CATEGORY_VALUES), name=op.f("ck_tickets_category_allowed")
        ),
        sa.CheckConstraint(
            _in_values("priority", PRIORITY_VALUES), name=op.f("ck_tickets_priority_allowed")
        ),
        sa.CheckConstraint(
            _in_values("status", STATUS_VALUES), name=op.f("ck_tickets_status_allowed")
        ),
        sa.CheckConstraint(
            "char_length(customer_email_snapshot) BETWEEN 3 AND 254",
            name=op.f("ck_tickets_customer_email_snapshot_length"),
        ),
        sa.CheckConstraint(
            "customer_email_snapshot = lower(customer_email_snapshot)",
            name=op.f("ck_tickets_customer_email_snapshot_lowercase"),
        ),
        sa.CheckConstraint(
            "order_id IS NULL OR char_length(order_id) BETWEEN 1 AND 64",
            name=op.f("ck_tickets_order_id_length"),
        ),
        sa.CheckConstraint(
            "char_length(subject) BETWEEN 1 AND 200", name=op.f("ck_tickets_subject_length")
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 10000",
            name=op.f("ck_tickets_description_length"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_tickets_version_positive")),
        sa.ForeignKeyConstraint(
            ["assigned_agent_id"], ["users.id"], name="fk_tickets_assigned_agent_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name="fk_tickets_created_by_id_users"
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], name="fk_tickets_customer_id_users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
        sa.UniqueConstraint("ticket_sequence", name="uq_tickets_ticket_sequence"),
    )
    op.create_index(
        "ix_tickets_assignee_status_updated",
        "tickets",
        ["assigned_agent_id", "status", sa.text("updated_at DESC"), "id"],
    )
    op.create_index("ix_tickets_created_by_id", "tickets", ["created_by_id"])
    op.create_index(
        "ix_tickets_customer_public_updated",
        "tickets",
        ["customer_id", sa.text("public_updated_at DESC"), "id"],
    )
    op.create_index(
        "ix_tickets_status_updated", "tickets", ["status", sa.text("updated_at DESC"), "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_tickets_status_updated", table_name="tickets")
    op.drop_index("ix_tickets_customer_public_updated", table_name="tickets")
    op.drop_index("ix_tickets_created_by_id", table_name="tickets")
    op.drop_index("ix_tickets_assignee_status_updated", table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("auth_throttles")
    op.drop_index("ix_auth_sessions_user_id_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("users")
    op.execute(sa.schema.DropSequence(sa.Sequence("ticket_sequence_seq")))
