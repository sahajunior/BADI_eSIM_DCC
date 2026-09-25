# ADR 0005: SLA deadlines as persisted per-cycle facts

- **Status:** Implemented in Phase 6 (migration `0003_sla_cycles`).
- **Context:** Support teams need first-response and resolution targets, but a target
  must survive priority changes, waiting states, reopening, timezone/DST, and repeated
  background scans without double-counting.
- **Decision:** Persist a `ticket_sla_cycles` row per cycle with due timestamps, met/
  breach timestamps, accumulated pause seconds, and a versioned policy string. Compute
  states from stored facts and an injectable "now". Opening a ticket starts cycle 1;
  resolving/closing completes it; reopening starts a new cycle while the historical
  first-response result stays on the old cycle. Priority changes accrue eligible time
  and re-target without resetting the clock. A worker marks breaches once each and
  emits staff-only audit/outbox rows.
- **Rejected:** Deriving deadlines from current fields only (loses history); storing a
  single mutable "due" with no pause accounting; treating WAITING_FOR_PROVIDER as a
  pause (it would hide overdue work); business-hour calendars in this phase (deferred).
- **Consequences:** Time arithmetic is UTC-duration based and timezone/DST independent.
  Policy changes need a new version string; existing cycle rows keep their version.
  Breach scanning is idempotent because a breach timestamp is written at most once.
- **Verification:** `tests/unit/test_sla.py` (boundaries, pause, provider wait, accrual,
  timezone, completion) and `tests/integration/test_sla_phase6.py` (pause/resume,
  priority change, reopen cycle, idempotent worker scan).
