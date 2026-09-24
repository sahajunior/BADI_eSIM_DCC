from __future__ import annotations

import os
from typing import Any

import pytest
from alembic import command
from sqlalchemy.sql.elements import TextClause

from app import migrate

ADMIN_DSN = "postgresql+asyncpg://admin:secret@localhost:5432/badi"
APP_PASSWORD = "random-runtime-password"


@pytest.mark.asyncio
async def test_run_rejects_non_development_environment(capsys: pytest.CaptureFixture[str]) -> None:
    called = False

    def upgrade(_url: str) -> None:
        nonlocal called
        called = True

    exit_code = await migrate.run(
        {
            "BADI_ENVIRONMENT": "production",
            "BADI_MIGRATION_DATABASE_URL": ADMIN_DSN,
            "BADI_APP_DB_PASSWORD": APP_PASSWORD,
        },
        upgrade=upgrade,
        provision=lambda _url, _password: None,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert not called
    assert "development" in captured.err
    assert "secret" not in captured.err
    assert APP_PASSWORD not in captured.err


@pytest.mark.asyncio
async def test_run_requires_admin_dsn_and_app_password() -> None:
    assert await migrate.run({}, upgrade=lambda _url: None, provision=lambda _u, _p: None) == 2
    assert (
        await migrate.run(
            {"BADI_MIGRATION_DATABASE_URL": ADMIN_DSN, "BADI_APP_DB_PASSWORD": "short"},
            upgrade=lambda _url: None,
            provision=lambda _u, _p: None,
        )
        == 2
    )


@pytest.mark.asyncio
async def test_run_invokes_upgrade_then_provision_with_valid_inputs() -> None:
    calls: list[tuple[str, str, str | None]] = []

    def upgrade(url: str) -> None:
        calls.append(("upgrade", url, None))

    async def provision(url: str, password: str) -> None:
        calls.append(("provision", url, password))

    exit_code = await migrate.run(
        {
            "BADI_ENVIRONMENT": "test",
            "BADI_MIGRATION_DATABASE_URL": ADMIN_DSN,
            "BADI_APP_DB_PASSWORD": APP_PASSWORD,
        },
        upgrade=upgrade,
        provision=provision,
    )

    assert exit_code == 0
    assert calls == [("upgrade", ADMIN_DSN, None), ("provision", ADMIN_DSN, APP_PASSWORD)]


def test_scoped_database_url_restores_previous_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BADI_DATABASE_URL", "postgresql+asyncpg://old@localhost/db")

    with migrate.scoped_database_url(ADMIN_DSN):
        assert os.environ["BADI_DATABASE_URL"] == ADMIN_DSN

    assert os.environ["BADI_DATABASE_URL"] == "postgresql+asyncpg://old@localhost/db"


def test_run_alembic_upgrade_scopes_admin_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_upgrade(_config: Any, revision: str) -> None:
        seen["revision"] = revision
        seen["database_url"] = os.environ["BADI_DATABASE_URL"]

    monkeypatch.setattr(command, "upgrade", fake_upgrade)
    migrate.run_alembic_upgrade(ADMIN_DSN)

    assert seen == {"revision": "head", "database_url": ADMIN_DSN}


@pytest.mark.asyncio
async def test_create_runtime_role_uses_postgres_literal_format_for_password() -> None:
    connection = RecordingConnection(exists=False)

    await migrate.create_runtime_role_if_missing(connection, "p'ass secret")  # type: ignore[arg-type]

    statements = [statement for statement, _params in connection.calls]
    assert any("format('%L', CAST(:password AS text))" in statement for statement in statements)
    assert all("p'ass secret" not in statement for statement in statements)
    assert connection.calls[1][1] == {"password": "p'ass secret"}
    assert connection.executed_dynamic_sql == ["CREATE ROLE badi_app LOGIN PASSWORD 'quoted'"]


@pytest.mark.asyncio
async def test_create_runtime_role_does_not_reset_existing_role_password() -> None:
    connection = RecordingConnection(exists=True)

    await migrate.create_runtime_role_if_missing(connection, APP_PASSWORD)  # type: ignore[arg-type]

    assert len(connection.calls) == 1
    assert connection.executed_dynamic_sql == []


@pytest.mark.asyncio
async def test_grant_runtime_privileges_are_stable_and_not_schema_wide() -> None:
    engine = RecordingEngine(exists=True)

    await migrate.grant_runtime_privileges(engine, APP_PASSWORD)  # type: ignore[arg-type]

    statements = [statement for statement, _params in engine.connection.calls]
    grant_statements = [statement for statement in statements if statement.startswith("GRANT")]
    assert "GRANT USAGE ON SCHEMA public TO badi_app" in grant_statements
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE users TO badi_app" in grant_statements
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE tickets TO badi_app" in grant_statements
    assert "GRANT USAGE, SELECT ON SEQUENCE ticket_sequence_seq TO badi_app" in grant_statements
    assert all("ALL" not in statement for statement in grant_statements)
    assert all("DEFAULT PRIVILEGES" not in statement for statement in grant_statements)
    assert all("SUPERUSER" not in statement for statement in grant_statements)


class RecordingConnection:
    def __init__(self, *, exists: bool) -> None:
        self.exists = exists
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.executed_dynamic_sql: list[str] = []

    async def scalar(self, clause: TextClause, params: dict[str, str] | None = None) -> bool | str:
        statement = str(clause)
        self.calls.append((statement, params or {}))
        if "SELECT EXISTS" in statement:
            return self.exists
        if "CREATE ROLE" in statement:
            return "CREATE ROLE badi_app LOGIN PASSWORD 'quoted'"
        if "GRANT CONNECT" in statement:
            return "GRANT CONNECT ON DATABASE badi TO badi_app"
        if "rolsuper" in statement or "has_schema_privilege" in statement:
            return False
        raise AssertionError(f"unexpected scalar statement: {statement}")

    async def execute(self, clause: TextClause) -> None:
        statement = str(clause)
        self.calls.append((statement, {}))
        if statement.startswith("CREATE ROLE"):
            self.executed_dynamic_sql.append(statement)

    async def exec_driver_sql(self, statement: str) -> None:
        await self.execute(TextClause(statement))


class RecordingBegin:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> RecordingConnection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class RecordingEngine:
    def __init__(self, *, exists: bool) -> None:
        self.connection = RecordingConnection(exists=exists)

    def begin(self) -> RecordingBegin:
        return RecordingBegin(self.connection)
