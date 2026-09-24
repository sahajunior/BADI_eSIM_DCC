.DEFAULT_GOAL := help
.PHONY: help env setup dev down logs db test-db migrate seed provision dev-api dev-web lint typecheck test test-setup test-backend test-frontend test-e2e build smoke auth-smoke realtime-smoke dev-worker outbox-once check audit reset-demo cleanup-attachments benchmark backup-restore-check

help:
	@printf '%s\n' 'Development commands:' '  make setup       Prepare private env and install locked dependencies' '  make dev         Build, migrate, and start the Docker stack' '  make migrate     Upgrade local app DB schema' '  make seed        Add idempotent synthetic demo users/tickets' '  make reset-demo CONFIRM=yes  Destructively reset the local demo database' '  make provision ARGS="..."  Provision an account with password prompt' '  make down        Stop stack (keeps app database volume)' '  make logs        Follow stack logs' '  make dev-api     Hot-reload API (run make migrate first)' '  make dev-web     Hot-reload frontend (run API first)' '  make check       Lint, types, isolated DB tests, and builds' '  make audit       Dependency vulnerability scans (npm audit + pip-audit)' '  make benchmark   Read-only latency/throughput measurement (local stack)' '  make backup-restore-check CONFIRM=yes  Backup/restore rehearsal into a scratch DB' '  make test-e2e    Real-stack browser smoke (run make dev first)' '  make smoke       HTTP smoke checks (run make dev first)' '  make realtime-smoke  Message/privacy/SSE checks through the actual proxy' '  make dev-worker  Run the outbox worker locally'

env:
	python3 scripts/setup-env.py

setup: env
	cd backend && uv sync --locked
	cd frontend && npm ci

dev: env
	docker compose up --build --detach --wait

down:
	docker compose --profile test down

reset-demo:
	@test "$(CONFIRM)" = "yes" || (printf '%s\n' 'Refusing: this removes the local database volume and all local data.' 'Re-run as: make reset-demo CONFIRM=yes'; exit 1)
	docker compose --profile test down --volumes
	$(MAKE) dev
	$(MAKE) seed

logs:
	docker compose logs --follow

db: env
	docker compose up --detach --wait db

test-db: env
	docker compose --profile test up --detach --wait db-test

migrate: db
	cd backend && uv run --locked --env-file ../.env python -m app.migrate

seed: migrate
	cd backend && uv run --locked --env-file ../.env python -m app.seed --demo

provision: migrate
	cd backend && uv run --locked --env-file ../.env python -m app.provision $(ARGS)

dev-api: env
	cd backend && uv run --env-file ../.env uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

dev-worker: env
	cd backend && uv run --locked --env-file ../.env python -m app.outbox

outbox-once: migrate
	cd backend && uv run --locked --env-file ../.env python -m app.outbox --once

cleanup-attachments: migrate
	cd backend && uv run --locked --env-file ../.env python -m app.cleanup

dev-web:
	cd frontend && npm run dev

lint:
	cd backend && uv run --locked ruff check . && uv run --locked ruff format --check .
	cd backend && uv run --locked ruff check ../scripts && uv run --locked ruff format --check ../scripts
	cd frontend && npm run lint

typecheck:
	cd backend && uv run --locked mypy
	cd frontend && npm run typecheck

test: test-setup test-backend test-frontend

test-setup:
	python3 -m unittest discover -s scripts -p 'test_*.py'

test-backend: test-db
	cd backend && uv run --locked --env-file ../.env pytest

test-frontend:
	cd frontend && npm run test

test-e2e:
	cd frontend && npm run test:e2e

build:
	cd frontend && npm run build
	docker compose build

smoke:
	./scripts/smoke.sh

auth-smoke: seed
	cd backend && PYTHONPATH=. uv run --locked --env-file ../.env python ../scripts/auth-smoke.py

realtime-smoke: seed
	cd backend && PYTHONPATH=. uv run --locked --env-file ../.env python ../scripts/realtime-smoke.py

check: lint typecheck test build

audit:
	cd frontend && npm audit --audit-level=high
	cd backend && uv export --locked --no-dev --no-hashes -o /tmp/badi-req.txt && uvx pip-audit==2.9.0 --strict -r /tmp/badi-req.txt

benchmark:
	cd backend && uv run --locked --env-file ../.env python ../scripts/benchmark.py

backup-restore-check:
	./scripts/backup-restore-check.sh CONFIRM=$(CONFIRM)
