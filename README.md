# BADI eSIM Support Ticketing

A full-stack support ticketing module built for the BADI eSIM interview assignment.
Customers can open and follow support cases, while support agents can manage queues,
assign ownership, update workflow state, reply publicly, and add private internal notes.

This repository is an independent assignment implementation. It is not an official
BADI or Digital Cloud Communications production service.

## What is implemented

### Customer workspace

- Sign in with a provisioned account.
- Create a ticket with category, subject, description, priority, and optional order reference.
- View and filter only the signed-in customer's tickets.
- Follow the public conversation and ticket status history.
- Send replies and receive agent replies without manually refreshing the page.
- View read-only mock order context when a ticket has an order reference.
- Configure reply-email preferences.

### Agent workspace

- Search, filter, and paginate the support queue.
- View unassigned tickets or tickets assigned to a specific agent.
- Open tickets with the full staff-visible conversation and audit history.
- Assign or reassign tickets.
- Change status and priority through an explicit workflow policy.
- Send customer-visible replies.
- Add internal notes that are never returned through customer API projections.
- Use saved replies, unread indicators, SLA timers, and the operational dashboard.
- Inspect failed notification deliveries and retry them.

### Platform behavior

- Readable sequence-backed ticket numbers beginning at `BD-1001`.
- Statuses: `OPEN`, `IN_PROGRESS`, `WAITING_FOR_CUSTOMER`,
  `WAITING_FOR_PROVIDER`, `RESOLVED`, and `CLOSED`.
- Priorities: `LOW`, `MEDIUM`, `HIGH`, and `URGENT`.
- Categories: `INSTALLATION`, `ACTIVATION`, `CONNECTIVITY`, `ORDER`,
  `TOPUP`, `REFUND`, and `OTHER`.
- Transactional audit events for status, priority, and assignment changes.
- Optimistic concurrency for staff updates through `ETag` and `If-Match`.
- Idempotency protection for retryable ticket and message creation.
- Real-time invalidation through authenticated Server-Sent Events (SSE), followed by
  canonical REST reconciliation.
- A PostgreSQL outbox worker so persisted changes and realtime notifications cannot
  disagree because of an in-process publish failure.
- Private attachments with authorization checks, size/type validation, and expiry of
  unattached uploads.
- Reply-notification records with retry/backoff; local development uses a redacted
  console email backend by default.

## Technology

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2, asyncpg, Alembic, Pydantic
- **Database:** PostgreSQL 17
- **Frontend:** React 19, TypeScript 6, Vite 8
- **Realtime:** Server-Sent Events with a PostgreSQL-backed outbox worker
- **Tests:** pytest, Vitest, Testing Library, Playwright
- **Local runtime:** Docker Compose and Nginx

Dependencies are reproducibly pinned in `backend/uv.lock` and
`frontend/package-lock.json`.

## Prerequisites

Install the following before running the project:

- Docker Engine with Docker Compose v2
- Python 3
- [uv](https://docs.astral.sh/uv/)
- Node.js 24+ and npm 11+
- GNU Make

PostgreSQL runs in Docker; no host PostgreSQL installation is required.

## Quick start

From the repository root:

```bash
make setup
make dev
make seed
```

`make setup` creates a private `.env` file when missing and installs the locked
backend and frontend dependencies. `make dev` builds the containers, starts
PostgreSQL, applies all migrations, provisions the restricted application database
role, and starts the API, worker, and web application. `make seed` adds repeatable
synthetic demo data.

Open:

- Application: <http://127.0.0.1:8080>
- API documentation: <http://127.0.0.1:8080/api/v1/docs>
- Direct API: <http://127.0.0.1:8000>

All exposed development ports bind to `127.0.0.1` by default.

Useful runtime commands:

```bash
make logs                       # follow container logs
make down                       # stop services and retain database data
make reset-demo CONFIRM=yes     # remove local demo data, rebuild, and reseed
```

`make reset-demo` is destructive to the local Compose database volume and therefore
requires the explicit confirmation argument shown above.

## Demo accounts

`make seed` creates the following accounts:

| Role | Email |
|---|---|
| Customer | `customer1@example.test` |
| Customer | `customer2@example.test` |
| Agent | `agent1@example.test` |
| Agent | `agent2@example.test` |
| Admin | `admin@example.test` |

The accounts share the random local-only password generated in `.env` as
`BADI_DEMO_PASSWORD`. Display it locally with:

```bash
grep '^BADI_DEMO_PASSWORD=' .env
```

Do not commit `.env` or publish the generated password. The seed command is
idempotent and does not overwrite existing account passwords or roles. If `.env` is
replaced while the existing database volume is retained, reset the demo database or
restore the original local credentials.

The seed includes seven tickets covering all categories, priorities, and workflow
states, along with assignments, public replies, internal notes, audit events, saved
replies, and mock order references.

## Demonstration flow

For a review, use two separate browser sessions:

1. Sign in as `customer1@example.test` and create a ticket.
2. Sign in as `agent1@example.test` in a private/incognito window.
3. Find the new ticket, assign it, and move it to **In progress**.
4. Reply as the agent and show the reply appearing in the customer session without a reload.
5. Reply as the customer and show the update appearing in the agent session.
6. Add an internal note and confirm it is absent from the customer session.
7. Show the audit history, SLA information, and agent dashboard.
8. Resolve the ticket.

For a predictable demonstration, run `make reset-demo CONFIRM=yes` beforehand.

## Verification

### Complete static, unit, integration, and build checks

```bash
make check
```

This runs:

- Ruff lint and formatting checks
- ESLint
- strict mypy
- TypeScript type checking
- setup-script unit tests
- backend unit and PostgreSQL integration tests
- frontend component tests
- frontend production build
- Docker image builds

Backend integration tests use a separate disposable `badi_test` database on port
`55433`; they do not use the application database.

### Running-stack checks

```bash
make dev
make seed
make smoke
make auth-smoke
make realtime-smoke
```

These verify web/API availability, database readiness, login, CSRF enforcement,
customer isolation, staff authorization, logout, bidirectional realtime replies,
internal-note privacy, and retry safety through the actual Nginx/API/worker stack.

### Browser tests

Install Chromium once, then run Playwright against the seeded stack:

```bash
cd frontend
npx playwright install chromium
cd ..
make test-e2e
```

The browser suite covers desktop and mobile layouts, customer/agent lifecycle,
authorization, accessibility, attachments, dashboard, order context, saved replies,
session revocation, settings, SLA display, and realtime reconnection.

## API overview

All application routes are versioned under `/api/v1`.

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/auth/login` | Create an authenticated session |
| `POST` | `/auth/logout` | Revoke the current session |
| `GET` | `/auth/me` | Return the current identity |
| `POST` | `/tickets` | Create a customer or agent ticket |
| `GET` | `/tickets` | Search, filter, and paginate visible tickets |
| `GET` | `/tickets/{id}` | Read a role-scoped ticket projection |
| `PATCH` | `/tickets/{id}` | Update status, priority, or assignment as staff |
| `GET` | `/tickets/{id}/messages` | Read visible conversation messages |
| `POST` | `/tickets/{id}/messages` | Add a public reply or staff internal note |
| `GET` | `/tickets/{id}/events` | Read staff audit history |
| `GET` | `/tickets/{id}/public-history` | Read customer-visible status history |
| `GET` | `/tickets/{id}/sla` | Read the active staff SLA cycle |
| `POST` | `/tickets/{id}/attachments` | Stage an authorized attachment |
| `GET` | `/attachments/{id}/download` | Download after message-visibility authorization |
| `GET` | `/events` | Authenticated SSE invalidation stream |
| `GET` | `/dashboard/summary` | Staff queue and SLA summary |
| `GET` | `/mock/orders/{order_id}` | Read-only mock order context |

`GET /tickets` supports status, priority, category, assigned-agent, unassigned,
search, limit, and signed-cursor pagination where permitted by the caller's role.
OpenAPI documentation is available at `/api/v1/docs` while the stack is running.

## Database migrations

Alembic migrations are applied automatically by the one-shot `migrate` Compose
service before the API and worker start.

| Revision | Purpose |
|---|---|
| `0001_identity_tickets` | Users, sessions, throttles, tickets, enums, and ticket numbering |
| `0002_conversations` | Messages, audit events, outbox events, and idempotency records |
| `0003_sla_cycles` | Persisted SLA cycles and deadline state |
| `0004_saved_replies` | Agent-owned saved reply templates |
| `0005_user_ticket_state` | Per-user ticket seen/unread state |
| `0006_attachments` | Private attachment metadata and lifecycle state |
| `0007_notifications` | Notification preferences and durable delivery records |

Manual migration command:

```bash
make migrate
```

## Architecture and important trade-offs

### Modular monolith

The API remains one FastAPI application because ticket workflow, authorization,
audit history, and outbox writes share transactional boundaries. Splitting these
operations across services would add distributed consistency work without helping the
assignment's scale. The notification/outbox worker is a separate process, but it uses
the same codebase and database.

### PostgreSQL as the source of truth

Ticket numbers use a PostgreSQL sequence rather than `MAX(...) + 1`; gaps are accepted
in exchange for safe concurrency. Ticket changes, audit events, idempotency results,
and outbox records are committed atomically.

### REST writes with SSE invalidation

REST remains canonical for reads and writes. SSE delivers minimal authorized
invalidation events, after which the browser refetches role-safe state. This avoids
placing sensitive message contents on a broadcast channel and makes reconnects
converge on database state. The trade-off is that the current stream does not provide
a durable `Last-Event-ID` replay contract.

### Server-enforced privacy

Customers and staff receive different response projections. Customer ownership and
internal-note visibility are enforced in backend queries and serialization, not only
by hiding frontend controls. Authentication uses provisioned accounts, Argon2id
password hashes, opaque server-side sessions, HttpOnly cookies, CSRF tokens, exact
origin checks, and request throttles.

### Explicit workflow and concurrency

Status transitions are validated by a domain policy. Staff mutations use versions and
`If-Match` to reject stale edits instead of silently accepting last-write-wins changes.
Reopening a closed ticket requires a reason.

More detailed decision records are available in `docs/decisions/`.

## Assumptions

- Accounts are provisioned by a trusted administrator; public registration and password
  recovery are outside the assignment scope.
- Customers can access only tickets attached to their authenticated user ID. Email
  addresses alone do not grant access.
- Agents and administrators are trusted support staff. The current admin role has staff
  capabilities but no account-management interface.
- The system represents one support organization and one customer identity per account.
- Order information is intentionally mocked and read-only; no real payment, carrier,
  or eSIM provider integration is performed.
- SLA targets use elapsed UTC duration rather than business calendars or regional holidays.
- Local demo data is synthetic and must not be mixed with real customer data.

## Known limitations

- Production mode intentionally fails closed. The provided Compose stack is for local
  development, testing, and demonstrations only.
- There is no public signup, invitation flow, password reset, or account-management UI.
- The default email backend writes a redacted delivery line to logs; SMTP requires
  explicit configuration and has no bundled mail provider.
- Attachment storage is local and private. Content signature checks reject unsupported
  or suspicious files, but they are not a substitute for a production malware scanner.
- Search uses database substring matching rather than PostgreSQL full-text search.
- SLA calculations do not implement business hours, holidays, or escalation schedules.
- SSE provides invalidation and reconciliation, not durable event replay, typing
  indicators, presence, or read receipts.
- No hosted environment or hosted CI result is claimed by this repository.

## What I would improve with more time

1. Add a hardened deployment profile with HTTPS, secure cookies, strict proxy headers,
   managed secrets, backups, restore drills, monitoring, and alerting.
2. Add verified invitations, password reset, session management, and account recovery.
3. Move attachments to private object storage and integrate a real malware-scanning service.
4. Integrate a transactional email provider with delivery webhooks and operational alerts.
5. Add business-calendar SLA policies and configurable escalation rules.
6. Add PostgreSQL full-text search and saved queue views for larger ticket volumes.
7. Add a hosted CI pipeline that runs the same locked checks documented above.
8. Perform accessibility testing with assistive technology and broader cross-browser testing.

## Project structure

```text
backend/
  app/                     FastAPI application and domain modules
  migrations/versions/     Alembic database migrations
  tests/                   Unit and PostgreSQL integration tests
frontend/
  src/                     React application and component tests
  e2e/                     Playwright end-to-end tests
infra/proxy/                Nginx same-origin reverse proxy
scripts/                    Setup, smoke, benchmark, and operational checks
compose.yaml                Local DB, migration, API, worker, test DB, and web services
Makefile                    Reproducible setup, run, test, and maintenance commands
```

## Environment and security notes

`make setup` generates `.env` with file mode `0600`. The file contains database,
authentication, and demo credentials and is excluded from Git. `.env.example` documents
the expected keys without containing usable secrets.

Important settings include:

| Variable | Purpose |
|---|---|
| `POSTGRES_PORT` | Host PostgreSQL port; default `55432` |
| `TEST_POSTGRES_PORT` | Disposable test database port; default `55433` |
| `API_PORT` | Direct API port; default `8000` |
| `WEB_PORT` | Same-origin web/proxy port; default `8080` |
| `BADI_AUTH_SECRET` | Session and CSRF signing secret |
| `BADI_DEMO_PASSWORD` | Shared local demo password |
| `BADI_ALLOWED_ORIGINS` | Exact origins permitted for cookie-authenticated requests |
| `BADI_ATTACHMENT_DIR` | Private local attachment directory |
| `BADI_EMAIL_BACKEND` | `console`, `disabled`, or `smtp` |
| `BADI_PUBLIC_BASE_URL` | Base URL used in notification links |

Never commit `.env`, Playwright authentication state, attachment data, database dumps,
or real customer information. These paths are excluded by `.gitignore`.

## Troubleshooting

- **Docker is unavailable:** start Docker and verify `docker info` succeeds.
- **A port is already in use:** stop the conflicting process or update the port and
  corresponding host-side URL in `.env`.
- **Database authentication fails after replacing `.env`:** the existing volume retains
  its original credentials. Restore the previous `.env` or deliberately reset the local
  demo with `make reset-demo CONFIRM=yes`.
- **The UI opens without demo tickets:** run `make seed`.
- **The browser suite cannot launch Chromium:** run
  `cd frontend && npx playwright install chromium`.
- **Live updates reconnect repeatedly:** inspect `docker compose ps` and
  `docker compose logs api worker web`; REST reconciliation continues while the stream
  reconnects.
