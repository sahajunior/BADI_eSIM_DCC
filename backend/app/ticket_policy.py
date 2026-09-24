"""Ticket workflow policy shared by Phase 4 ticket routes."""

from app.errors import ApiError
from app.models import TicketStatus

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    TicketStatus.OPEN.value: {
        TicketStatus.IN_PROGRESS.value,
        TicketStatus.WAITING_FOR_CUSTOMER.value,
        TicketStatus.WAITING_FOR_PROVIDER.value,
        TicketStatus.RESOLVED.value,
    },
    TicketStatus.IN_PROGRESS.value: {
        TicketStatus.WAITING_FOR_CUSTOMER.value,
        TicketStatus.WAITING_FOR_PROVIDER.value,
        TicketStatus.RESOLVED.value,
    },
    TicketStatus.WAITING_FOR_CUSTOMER.value: {
        TicketStatus.IN_PROGRESS.value,
        TicketStatus.WAITING_FOR_PROVIDER.value,
        TicketStatus.RESOLVED.value,
    },
    TicketStatus.WAITING_FOR_PROVIDER.value: {
        TicketStatus.IN_PROGRESS.value,
        TicketStatus.WAITING_FOR_CUSTOMER.value,
        TicketStatus.RESOLVED.value,
    },
    TicketStatus.RESOLVED.value: {TicketStatus.IN_PROGRESS.value, TicketStatus.CLOSED.value},
    TicketStatus.CLOSED.value: {TicketStatus.IN_PROGRESS.value},
}


def validate_status_transition(old_status: str, new_status: str, reason: str | None) -> None:
    if old_status == new_status:
        return
    if new_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
        raise ApiError(
            409, "invalid_transition", f"Cannot move ticket from {old_status} to {new_status}."
        )
    if (
        old_status == TicketStatus.CLOSED.value
        and new_status == TicketStatus.IN_PROGRESS.value
        and not reason
    ):
        raise ApiError(
            422, "reopen_reason_required", "Reopening a closed ticket requires a reason."
        )
