"""Read-only mock order context.

There is no real eSIM provider integration. Orders are a small deterministic
fixture; unknown references are reported as not found rather than fabricated.
Ownership is part of the fixture so a customer can only resolve their own orders
and ticket creation rejects another customer's order reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.errors import ApiError
from app.models import Role, User

router = APIRouter(prefix="/api/v1/mock", tags=["orders"])


@dataclass(frozen=True)
class MockOrder:
    order_id: str
    owner_email: str
    destination: str
    package: str
    status: str
    esim_status: str


MOCK_ORDERS: dict[str, MockOrder] = {
    # Assignment example.
    "ORD-10293": MockOrder(
        "ORD-10293", "customer1@example.test", "Turkey", "10 GB", "completed", "installed"
    ),
    # Fixtures matching the deterministic demo seed tickets.
    "ORD-DEMO-1001": MockOrder(
        "ORD-DEMO-1001", "customer1@example.test", "Turkey", "10 GB", "completed", "installed"
    ),
    "ORD-DEMO-1002": MockOrder(
        "ORD-DEMO-1002", "customer2@example.test", "Japan", "5 GB", "completed", "installed"
    ),
    "ORD-DEMO-1004": MockOrder(
        "ORD-DEMO-1004",
        "customer2@example.test",
        "United States",
        "20 GB",
        "completed",
        "installed",
    ),
    "ORD-DEMO-1005": MockOrder(
        "ORD-DEMO-1005", "customer1@example.test", "France", "5 GB", "completed", "installed"
    ),
}


def lookup_order(order_id: str) -> MockOrder | None:
    return MOCK_ORDERS.get(order_id)


def order_owner(order_id: str) -> str | None:
    order = lookup_order(order_id)
    return order.owner_email if order is not None else None


class OrderSummary(BaseModel):
    order_id: str
    destination: str
    package: str
    status: str
    esim_status: str


@router.get("/orders/{order_id}", response_model=OrderSummary)
async def read_order(
    order_id: str,
    user: Annotated[User, Depends(get_current_user)],
) -> OrderSummary:
    try:
        order = lookup_order(order_id)
    except Exception as exc:
        raise ApiError(503, "order_lookup_unavailable", "Order lookup is unavailable.") from exc
    # Unknown orders and other customers' orders are indistinguishable to avoid
    # confirming that an order reference exists.
    if order is None or (user.role == Role.CUSTOMER and order.owner_email != user.email):
        raise ApiError(404, "order_not_found", "Order not found.")
    return OrderSummary(
        order_id=order.order_id,
        destination=order.destination,
        package=order.package,
        status=order.status,
        esim_status=order.esim_status,
    )
