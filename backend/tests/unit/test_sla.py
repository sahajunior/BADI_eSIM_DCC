"""SLA policy math: boundaries, pause, provider wait, priority accrual, timezone."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

from app.models import TicketSlaCycle
from app.sla import (
    DUE_SOON_FRACTION,
    POLICY_VERSION,
    eligible_resolution_seconds,
    first_response_target,
    first_response_timer,
    resolution_target,
    resolution_timer,
)

BASE = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def make_cycle(priority: str = "HIGH", **overrides: object) -> TicketSlaCycle:
    cycle = TicketSlaCycle(
        ticket_id=uuid4(),
        cycle_number=1,
        policy_version=POLICY_VERSION,
        priority=priority,
        first_response_due_at=BASE + timedelta(seconds=first_response_target(priority)),
        resolution_started_at=BASE,
        resolution_due_at=BASE + timedelta(seconds=resolution_target(priority)),
    )
    for key, value in overrides.items():
        setattr(cycle, key, value)
    return cycle


def test_first_response_due_soon_and_overdue_boundaries() -> None:
    cycle = make_cycle("HIGH")  # target 3600s, due-soon window = final 720s
    target = first_response_target("HIGH")
    due = cycle.first_response_due_at
    assert due is not None

    on_track = first_response_timer(cycle, due - timedelta(seconds=DUE_SOON_FRACTION * target + 1))
    boundary = first_response_timer(cycle, due - timedelta(seconds=DUE_SOON_FRACTION * target))
    at_due = first_response_timer(cycle, due)
    overdue = first_response_timer(cycle, due + timedelta(seconds=1))

    assert on_track.state == "on_track"
    assert boundary.state == "due_soon"  # exactly 20% remaining is due-soon
    assert at_due.state == "overdue"  # zero remaining is overdue
    assert overdue.state == "overdue"
    assert overdue.remaining_seconds == 0


def test_met_first_response_does_not_depend_on_deadline() -> None:
    cycle = make_cycle("HIGH", first_response_at=BASE + timedelta(minutes=11))
    timer = first_response_timer(cycle, BASE + timedelta(days=3))
    assert timer.state == "met"
    assert timer.met_at == BASE + timedelta(minutes=11)


def test_resolution_pause_excludes_waiting_for_customer() -> None:
    cycle = make_cycle("MEDIUM")
    due = cycle.resolution_due_at
    cycle.resolution_pause_started_at = BASE + timedelta(hours=1)

    paused = resolution_timer(cycle, BASE + timedelta(hours=2))
    assert paused.state == "paused"
    assert paused.remaining_seconds is None
    # The pause shifts the effective deadline by the paused duration.
    assert paused.due_at == due + timedelta(hours=1)


def test_provider_wait_does_not_pause_or_hide_overdue() -> None:
    cycle = make_cycle("MEDIUM")
    # WAITING_FOR_PROVIDER is an active state; no pause marker is set for it.
    assert cycle.resolution_pause_started_at is None
    timer = resolution_timer(cycle, cycle.resolution_due_at + timedelta(minutes=1))
    assert timer.state == "overdue"


def test_eligible_resolution_seconds_accrues_pause_and_current_pause() -> None:
    cycle = make_cycle("MEDIUM", resolution_paused_seconds=3600)
    cycle.resolution_pause_started_at = BASE + timedelta(hours=5)
    # 6h wall clock, 1h previously paused, 1h currently paused => 4h eligible.
    eligible = eligible_resolution_seconds(cycle, BASE + timedelta(hours=6))
    assert eligible == 4 * 3600


def test_deadlines_are_timezone_independent() -> None:
    cycle = make_cycle("URGENT")
    utc_now = cycle.resolution_due_at - timedelta(minutes=7)
    other_tz_now = utc_now.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert (
        resolution_timer(cycle, utc_now).remaining_seconds
        == resolution_timer(cycle, other_tz_now).remaining_seconds
    )


def test_completed_resolution_is_met_only_within_deadline() -> None:
    cycle = make_cycle("LOW")
    due = cycle.resolution_due_at
    cycle.resolution_completed_at = due - timedelta(minutes=1)
    assert resolution_timer(cycle, BASE + timedelta(days=10)).state == "met"

    late = make_cycle("LOW")
    late.resolution_completed_at = late.resolution_due_at + timedelta(minutes=1)
    assert resolution_timer(late, BASE + timedelta(days=10)).state == "overdue"
