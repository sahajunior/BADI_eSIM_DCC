# ADR 0001: Keep the product a modular monolith

- **Status:** Accepted for Phase 0, 2026-09-21.
- **Context:** This is a greenfield personal project. The immediate need is a
  reproducible foundation, not distributed infrastructure.
- **Decision:** FastAPI + SQLAlchemy async PostgreSQL access; React + TypeScript +
  Vite; a single same-origin `/api/v1` boundary; Docker Compose for local operation.
  Use Python 3.12, Node 24, and PostgreSQL 17 release lines. Exact Python/frontend
  package graphs live in `backend/uv.lock` and `frontend/package-lock.json`.
- **Why:** Domain rules and transactional history belong together. The browser
  does not need a separate server-rendered application for these workflows.
- **Rejected:** Microservices, Redis, separate search infrastructure, and a UI kit
  before there is a demonstrated need; they add more failure/configuration paths.
- **Consequences:** Phase 0 contains API, database, and static web/proxy only. A
  worker arrives with real outbox work in Phase 3; migrations/schema in Phase 1.
  No fake no-op migration/seed/worker commands imply features that do not exist.
- **Verification:** Lockfile installs, production container builds, health tests,
  database-backed readiness, and real-stack browser smoke checks.
- **References:** [Vite guide](https://vite.dev/guide/),
  [SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html),
  [uv in Docker](https://docs.astral.sh/uv/guides/integration/docker/).

Phase 0 readiness means `SELECT 1` succeeds. Phase 1 now also verifies
the supported migration revision; connectivity alone is not sufficient.

## Dependency inventory

- FastAPI: HTTP routing, validation integration, OpenAPI; SQLAlchemy + asyncpg:
  asynchronous PostgreSQL connectivity; settings library: environment validation.
- React/React DOM: rendering; Vite + TypeScript: development and typed production
  build. No routing or server-query cache dependency until actual feature routes.
- Ruff/mypy/pytest/httpx and ESLint/Vitest/Testing Library/Playwright: development
  checks only. No runtime LLM, storage, email, or mobile SDK is installed in Phase 0.
- Review actual locked versions/licenses during upgrades. Lockfiles provide package
  repeatability; base image tags require deliberate refresh and verification too.

CI action revisions are pinned to verified upstream commits, with read-only job
permissions and checkout credential persistence disabled. Upgrade deliberately
using the [checkout](https://github.com/actions/checkout),
[setup-node](https://github.com/actions/setup-node), and
[setup-uv](https://github.com/astral-sh/setup-uv) upstream documentation.
