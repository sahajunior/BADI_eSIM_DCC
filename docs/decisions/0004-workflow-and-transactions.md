# ADR 0004: Explicit workflow and atomic history

- **Status:** Implemented (migrations `0001`–`0002`); verified in Phases 2–3.
- **Context:** Assignment, status changes, replies, and retries can race. An audit
  trail that disagrees with the saved ticket is worse than a simple feature set.
- **Decision:** Implement the transition table in `PLAN.MD` section 4 as a domain
  policy. Tickets begin OPEN with unique sequence-backed BD numbers. Status,
  priority, and assignment updates use If-Match/version checks, appropriate row
  locks, and atomic ticket/audit/outbox writes. No-op changes create no audit.
- **Rejected:** Arbitrary status strings; `MAX(number) + 1`; last-write-wins edits;
  writing audit entries after the ticket commits; duplicating the description as a
  conversation message.
- **Consequences:** Sequence gaps are acceptable. Public messages cannot race past
  a close. Message positions are allocated under the ticket lock. Idempotency keys
  protect creation/retry. Public and internal activity are separate projections.
- **Verification planned:** Transition table, stale updates, concurrent creation,
  reply/close races, rollback injection, no-op audits, and idempotent retries.
- **References:** [PostgreSQL isolation](https://www.postgresql.org/docs/current/transaction-iso.html),
  [SQLAlchemy version counters](https://docs.sqlalchemy.org/en/20/orm/versioning.html).

Phase 1 introduced an explicit Alembic migration and the Ticket model with enum
constraints, account ownership, unique sequence-backed numbering, version, and
separate public/internal activity timestamps. Phase 2 added the lifecycle routes,
transition policy, optimistic concurrency, and atomic audit/idempotency/outbox writes;
Phase 3 added messages and live invalidation. See
[Phase 2](../phase-2-verification.md) and [Phase 3](../phase-3-verification.md) evidence.
