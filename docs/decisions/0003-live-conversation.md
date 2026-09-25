# ADR 0003: REST writes plus SSE invalidation and reconciliation

- **Status:** Implemented end-to-end, 2026-09-22; backend delivery plus Phase 4 client reconciliation.
- **Context:** Replies must become visible in both browser sessions without refresh.
  Typing/presence/read receipts are not required.
- **Decision:** REST is canonical for persistence and reads. Authenticated SSE
  carries minimal authorized invalidations; clients fetch role-safe state. Persist
  an outbox atomically with mutations, use database wake-ups across API processes,
  resync on connection, and periodically reconcile active views.
- **Rejected:** In-memory-only broadcast; claiming NOTIFY is durable history;
  WebSockets without a needed duplex transport feature; promising event replay
  without a commit-safe sequence and retention contract.
- **Consequences:** Duplicate/lost invalidations are tolerated; message IDs are
  deduplicated and database state survives restarts. Initial SSE does not promise
  Last-Event-ID replay. Internal events never invalidate customer views.
- **Verification planned:** Two browser contexts, duplicate events, reconnect,
  subscribe races, revoked sessions, API/worker restarts, and proxy behavior.
- **References:** [SSE browser behavior](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events),
  [PostgreSQL NOTIFY](https://www.postgresql.org/docs/current/sql-notify.html).

The separate outbox worker and per-process PostgreSQL listener now deliver
minimal authorized invalidations through `/api/v1/events`. Initial/periodic resync
requires clients to fetch canonical REST data. No SSE replay IDs are emitted.
Per-account stream caps are process-local; worker transaction locks replace leases
for database-only NOTIFY delivery. See [contract](../realtime.md) and
[verification](../phase-3-verification.md). The Phase 2 lifecycle API and Phase 4
browser workflow are implemented; the Phase 4 client merges canonical REST state and
renders reconnect/resync states (see [Phase 4 evidence](../phase-4-verification.md)).
