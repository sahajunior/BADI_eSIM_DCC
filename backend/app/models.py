from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata


class Role(StrEnum):
    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    ADMIN = "ADMIN"


class TicketCategory(StrEnum):
    INSTALLATION = "INSTALLATION"
    ACTIVATION = "ACTIVATION"
    CONNECTIVITY = "CONNECTIVITY"
    ORDER = "ORDER"
    TOPUP = "TOPUP"
    REFUND = "REFUND"
    OTHER = "OTHER"


class TicketPriority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class Visibility(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"


class TicketStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_FOR_CUSTOMER = "WAITING_FOR_CUSTOMER"
    WAITING_FOR_PROVIDER = "WAITING_FOR_PROVIDER"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


ROLE_VALUES: Final[tuple[str, ...]] = tuple(role.value for role in Role)
TICKET_CATEGORY_VALUES: Final[tuple[str, ...]] = tuple(
    category.value for category in TicketCategory
)
TICKET_PRIORITY_VALUES: Final[tuple[str, ...]] = tuple(
    priority.value for priority in TicketPriority
)
TICKET_STATUS_VALUES: Final[tuple[str, ...]] = tuple(status.value for status in TicketStatus)
VISIBILITY_VALUES: Final[tuple[str, ...]] = tuple(visibility.value for visibility in Visibility)

ticket_sequence = Sequence("ticket_sequence_seq", start=1001)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_check_in("role", ROLE_VALUES), name="role_allowed"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("char_length(email) BETWEEN 3 AND 254", name="email_length"),
        CheckConstraint("char_length(display_name) BETWEEN 1 AND 100", name="display_name_length"),
        UniqueConstraint("email", name="uq_users_email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    tickets: Mapped[list[Ticket]] = relationship(
        back_populates="customer", foreign_keys="Ticket.customer_id"
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("char_length(token_hash) = 64", name="token_hash_length"),
        UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        Index("ix_auth_sessions_user_id_expires_at", "user_id", "expires_at"),
        Index("ix_auth_sessions_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_auth_sessions_user_id_users"),
        nullable=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuthThrottle(Base):
    __tablename__ = "auth_throttles"
    __table_args__ = (
        CheckConstraint("char_length(key_hash) = 64", name="key_hash_length"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(_check_in("category", TICKET_CATEGORY_VALUES), name="category_allowed"),
        CheckConstraint(_check_in("priority", TICKET_PRIORITY_VALUES), name="priority_allowed"),
        CheckConstraint(_check_in("status", TICKET_STATUS_VALUES), name="status_allowed"),
        CheckConstraint(
            "char_length(customer_email_snapshot) BETWEEN 3 AND 254",
            name="customer_email_snapshot_length",
        ),
        CheckConstraint(
            "customer_email_snapshot = lower(customer_email_snapshot)",
            name="customer_email_snapshot_lowercase",
        ),
        CheckConstraint(
            "order_id IS NULL OR char_length(order_id) BETWEEN 1 AND 64", name="order_id_length"
        ),
        CheckConstraint("char_length(subject) BETWEEN 1 AND 200", name="subject_length"),
        CheckConstraint("char_length(description) BETWEEN 1 AND 10000", name="description_length"),
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("ticket_sequence", name="uq_tickets_ticket_sequence"),
        Index(
            "ix_tickets_customer_public_updated",
            "customer_id",
            text("public_updated_at DESC"),
            "id",
        ),
        Index("ix_tickets_status_updated", "status", text("updated_at DESC"), "id"),
        Index(
            "ix_tickets_assignee_status_updated",
            "assigned_agent_id",
            "status",
            text("updated_at DESC"),
            "id",
        ),
        Index("ix_tickets_created_by_id", "created_by_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    ticket_sequence: Mapped[int] = mapped_column(
        Integer,
        ticket_sequence,
        nullable=False,
        server_default=text("nextval('ticket_sequence_seq')"),
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_tickets_customer_id_users"),
        nullable=False,
    )
    customer_email_snapshot: Mapped[str] = mapped_column(String(254), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_tickets_created_by_id_users"),
        nullable=False,
    )
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=TicketPriority.MEDIUM.value,
        server_default=TicketPriority.MEDIUM.value,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=TicketStatus.OPEN.value,
        server_default=TicketStatus.OPEN.value,
    )
    assigned_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_tickets_assigned_agent_id_users"),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    public_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    first_agent_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer: Mapped[User] = relationship(foreign_keys=[customer_id], back_populates="tickets")

    @property
    def ticket_number(self) -> str:
        return f"BD-{self.ticket_sequence}"


class TicketMessage(Base):
    __tablename__ = "ticket_messages"
    __table_args__ = (
        CheckConstraint(_check_in("sender_role_snapshot", ROLE_VALUES), name="sender_role_allowed"),
        CheckConstraint(_check_in("visibility", VISIBILITY_VALUES), name="visibility_allowed"),
        CheckConstraint("char_length(body) BETWEEN 1 AND 10000", name="body_length"),
        CheckConstraint("position >= 1", name="position_positive"),
        UniqueConstraint("ticket_id", "position", name="uq_ticket_messages_ticket_position"),
        Index("ix_ticket_messages_ticket_position", "ticket_id", "position"),
        Index(
            "ix_ticket_messages_ticket_public_position",
            "ticket_id",
            "position",
            postgresql_where=text("visibility = 'PUBLIC'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", name="fk_ticket_messages_ticket_id_tickets"),
        nullable=False,
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_ticket_messages_sender_id_users"),
        nullable=False,
    )
    sender_role_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (
        CheckConstraint(_check_in("visibility", VISIBILITY_VALUES), name="visibility_allowed"),
        CheckConstraint("char_length(event_type) BETWEEN 1 AND 40", name="event_type_length"),
        CheckConstraint(
            "field IS NULL OR char_length(field) BETWEEN 1 AND 40", name="field_length"
        ),
        CheckConstraint(
            "request_id IS NULL OR char_length(request_id) <= 36", name="request_id_length"
        ),
        Index("ix_ticket_events_ticket_created_id", "ticket_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", name="fk_ticket_events_ticket_id_tickets"),
        nullable=False,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_ticket_events_actor_id_users"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    field: Mapped[str | None] = mapped_column(String(40), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ticket_messages.id", name="fk_ticket_events_source_message_id_ticket_messages"),
        nullable=True,
    )
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    visibility: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=Visibility.INTERNAL.value,
        server_default=Visibility.INTERNAL.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(_check_in("visibility", VISIBILITY_VALUES), name="visibility_allowed"),
        CheckConstraint("char_length(kind) BETWEEN 1 AND 40", name="kind_length"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 200", name="last_error_length"
        ),
        Index(
            "ix_outbox_events_pending",
            "available_at",
            "id",
            postgresql_where=text("delivered_at IS NULL"),
        ),
        Index("ix_outbox_events_ticket_created", "ticket_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", name="fk_outbox_events_ticket_id_tickets"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)


class TicketSlaCycle(Base):
    __tablename__ = "ticket_sla_cycles"
    __table_args__ = (
        CheckConstraint("cycle_number >= 1", name="cycle_number_positive"),
        CheckConstraint("resolution_paused_seconds >= 0", name="paused_seconds_non_negative"),
        CheckConstraint(_check_in("priority", TICKET_PRIORITY_VALUES), name="priority_allowed"),
        UniqueConstraint("ticket_id", "cycle_number", name="uq_ticket_sla_cycles_ticket_cycle"),
        Index(
            "ix_ticket_sla_cycles_active_due",
            "is_active",
            "resolution_due_at",
            "id",
        ),
        Index("ix_ticket_sla_cycles_ticket", "ticket_id", "cycle_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", name="fk_ticket_sla_cycles_ticket_id_tickets"),
        nullable=False,
    )
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    priority: Mapped[str] = mapped_column(String(20), nullable=False)
    first_response_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_response_breached_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolution_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolution_paused_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    resolution_pause_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution_breached_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_notification_preferences_user_id_users"),
        primary_key=True,
    )
    email_on_reply: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("true")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(
            _check_in("status", ("PENDING", "SENT", "FAILED", "SKIPPED")),
            name="status_allowed",
        ),
        CheckConstraint(_check_in("channel", ("email", "queue")), name="channel_allowed"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 200", name="last_error_length"
        ),
        UniqueConstraint(
            "message_id", "recipient_id", "channel", name="uq_notification_deliveries_recipient"
        ),
        Index(
            "uq_notification_deliveries_queue",
            "message_id",
            "channel",
            unique=True,
            postgresql_where=text("recipient_id IS NULL"),
        ),
        Index("ix_notification_deliveries_pending", "status", "available_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", name="fk_notification_deliveries_ticket_id_tickets"),
        nullable=False,
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "ticket_messages.id", name="fk_notification_deliveries_message_id_ticket_messages"
        ),
        nullable=False,
    )
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_notification_deliveries_recipient_id_users"),
        nullable=True,
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PENDING", server_default="PENDING"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    provider_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint(
            _check_in("scan_state", ("PENDING", "CLEAN", "REJECTED")), name="scan_state_allowed"
        ),
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        CheckConstraint(
            "char_length(original_name) BETWEEN 1 AND 255", name="original_name_length"
        ),
        Index("ix_attachments_message", "message_id"),
        Index("ix_attachments_ticket", "ticket_id"),
        Index("ix_attachments_expiry", "scan_state", "expires_at"),
        UniqueConstraint("storage_key", name="uq_attachments_storage_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", name="fk_attachments_ticket_id_tickets"),
        nullable=False,
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ticket_messages.id", name="fk_attachments_message_id_ticket_messages"),
        nullable=True,
    )
    uploader_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_attachments_uploader_id_users"),
        nullable=False,
    )
    storage_key: Mapped[str] = mapped_column(String(64), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    detected_type: Mapped[str] = mapped_column(String(30), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    scan_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PENDING", server_default="PENDING"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserTicketState(Base):
    __tablename__ = "user_ticket_state"
    __table_args__ = (Index("ix_user_ticket_state_ticket", "ticket_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_user_ticket_state_user_id_users"),
        primary_key=True,
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", name="fk_user_ticket_state_ticket_id_tickets"),
        primary_key=True,
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SavedReply(Base):
    __tablename__ = "saved_replies"
    __table_args__ = (
        CheckConstraint("char_length(title) BETWEEN 1 AND 120", name="title_length"),
        CheckConstraint("char_length(body) BETWEEN 1 AND 10000", name="body_length"),
        CheckConstraint(
            "category IS NULL OR " + _check_in("category", TICKET_CATEGORY_VALUES),
            name="category_allowed",
        ),
        Index("ix_saved_replies_active_title", "is_active", "title"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_saved_replies_author_id_users"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("char_length(route) BETWEEN 1 AND 200", name="route_length"),
        CheckConstraint("char_length(key) BETWEEN 1 AND 128", name="key_length"),
        CheckConstraint("char_length(request_hash) = 64", name="request_hash_length"),
        CheckConstraint("char_length(result_type) BETWEEN 1 AND 20", name="result_type_length"),
        Index("ix_idempotency_records_expires_at", "expires_at"),
    )

    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_idempotency_records_actor_id_users"),
        primary_key=True,
    )
    route: Mapped[str] = mapped_column(String(200), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_type: Mapped[str] = mapped_column(String(20), nullable=False)
    result_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
