"""add conversation, audit, outbox, and idempotency tables

Revision ID: 0002_conversations
Revises: 0001_identity_tickets
Create Date: 2026-09-21 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_conversations"
down_revision: str | None = "0001_identity_tickets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_VALUES = ("CUSTOMER", "AGENT", "ADMIN")
VISIBILITY_VALUES = ("PUBLIC", "INTERNAL")


def _in_values(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    op.create_table(
        "ticket_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_role_snapshot", sa.String(length=20), nullable=False),
        sa.Column("visibility", sa.String(length=20), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            _in_values("sender_role_snapshot", ROLE_VALUES),
            name=op.f("ck_ticket_messages_sender_role_allowed"),
        ),
        sa.CheckConstraint(
            _in_values("visibility", VISIBILITY_VALUES),
            name=op.f("ck_ticket_messages_visibility_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 10000", name=op.f("ck_ticket_messages_body_length")
        ),
        sa.CheckConstraint("position >= 1", name=op.f("ck_ticket_messages_position_positive")),
        sa.ForeignKeyConstraint(
            ["sender_id"], ["users.id"], name="fk_ticket_messages_sender_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name="fk_ticket_messages_ticket_id_tickets"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_messages")),
        sa.UniqueConstraint("ticket_id", "position", name="uq_ticket_messages_ticket_position"),
    )
    op.create_index(
        "ix_ticket_messages_ticket_position",
        "ticket_messages",
        ["ticket_id", "position"],
        unique=False,
    )
    op.create_index(
        "ix_ticket_messages_ticket_public_position",
        "ticket_messages",
        ["ticket_id", "position"],
        unique=False,
        postgresql_where=sa.text("visibility = 'PUBLIC'"),
    )

    op.create_table(
        "ticket_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("field", sa.String(length=40), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", sa.String(length=36), nullable=True),
        sa.Column("visibility", sa.String(length=20), server_default="INTERNAL", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            _in_values("visibility", VISIBILITY_VALUES),
            name=op.f("ck_ticket_events_visibility_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(event_type) BETWEEN 1 AND 40",
            name=op.f("ck_ticket_events_event_type_length"),
        ),
        sa.CheckConstraint(
            "field IS NULL OR char_length(field) BETWEEN 1 AND 40",
            name=op.f("ck_ticket_events_field_length"),
        ),
        sa.CheckConstraint(
            "request_id IS NULL OR char_length(request_id) <= 36",
            name=op.f("ck_ticket_events_request_id_length"),
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_ticket_events_actor_id_users"),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["ticket_messages.id"],
            name="fk_ticket_events_source_message_id_ticket_messages",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name="fk_ticket_events_ticket_id_tickets"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_events")),
    )
    op.create_index(
        "ix_ticket_events_ticket_created_id",
        "ticket_events",
        ["ticket_id", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "outbox_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("visibility", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.CheckConstraint(
            _in_values("visibility", VISIBILITY_VALUES),
            name=op.f("ck_outbox_events_visibility_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(kind) BETWEEN 1 AND 40", name=op.f("ck_outbox_events_kind_length")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_outbox_events_attempts_non_negative")),
        sa.CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 200",
            name=op.f("ck_outbox_events_last_error_length"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name="fk_outbox_events_ticket_id_tickets"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
    )
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["available_at", "id"],
        unique=False,
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.create_index(
        "ix_outbox_events_ticket_created",
        "outbox_events",
        ["ticket_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "idempotency_records",
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("route", sa.String(length=200), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_type", sa.String(length=20), nullable=False),
        sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(route) BETWEEN 1 AND 200", name=op.f("ck_idempotency_records_route_length")
        ),
        sa.CheckConstraint(
            "char_length(key) BETWEEN 1 AND 128", name=op.f("ck_idempotency_records_key_length")
        ),
        sa.CheckConstraint(
            "char_length(request_hash) = 64",
            name=op.f("ck_idempotency_records_request_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(result_type) BETWEEN 1 AND 20",
            name=op.f("ck_idempotency_records_result_type_length"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name="fk_idempotency_records_actor_id_users"
        ),
        sa.PrimaryKeyConstraint("actor_id", "route", "key", name=op.f("pk_idempotency_records")),
    )
    op.create_index(
        "ix_idempotency_records_expires_at", "idempotency_records", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_records_expires_at", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    op.drop_index("ix_outbox_events_ticket_created", table_name="outbox_events")
    op.drop_index(
        "ix_outbox_events_pending",
        table_name="outbox_events",
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.drop_table("outbox_events")
    op.drop_index("ix_ticket_events_ticket_created_id", table_name="ticket_events")
    op.drop_table("ticket_events")
    op.drop_index(
        "ix_ticket_messages_ticket_public_position",
        table_name="ticket_messages",
        postgresql_where=sa.text("visibility = 'PUBLIC'"),
    )
    op.drop_index("ix_ticket_messages_ticket_position", table_name="ticket_messages")
    op.drop_table("ticket_messages")
