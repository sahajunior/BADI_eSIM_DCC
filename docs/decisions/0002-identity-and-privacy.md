# ADR 0002: Server-enforced identity before private ticket features

- **Status:** Implemented for the Phase 1 HTTP boundary, 2026-09-21.
- **Context:** Email addresses and ticket numbers are not authentication. Internal
  notes must remain private through every channel, not just hidden UI elements.
- **Decision:** Same-origin opaque HttpOnly cookie sessions, server-side session
  records, CSRF and origin checks, and customer/agent/admin policies. Separate
  customer and staff response projections. Provisioned accounts first.
- **Rejected:** Email-only ticket lookup; UI-only role controls; bearer tokens in
  localStorage; public registration before verification and abuse controls.
- **Consequences:** Phase 1 implements provisioned-account authentication and
  ownership-scoped read-only ticket details. Local-only Compose ports do not
  constitute a secure public deployment.
  Production mode fails closed until actual security configuration is designed.
- **Verification:** PostgreSQL-backed HTTP ownership/role negative tests, session
  rotation/expiry/revocation, concurrent login, CSRF/origin checks, rate limits and
  response-field visibility. Streaming/internal-note tests remain Phase 3 work.
- **Source:** `PLAN.MD` sections 3, 7, and 10.

The account ID, not a matching email string, will own a ticket. Customers cannot
submit another owner, sender, role, assignee, or INTERNAL message. The browser
receives no internal versions/activity timestamps. Administrative provisioning
must not be confused with unrestricted public registration.

Phase 1 uses Argon2id and database-backed opaque sessions/throttles. Only hashed
session tokens are stored. A session-bound HMAC token protects login and logout;
exact origin checks supplement SameSite cookies. See [security](../security.md)
for operational limits and [evidence](../phase-1-verification.md) for results.
