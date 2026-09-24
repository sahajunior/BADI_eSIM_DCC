from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final, cast

from alembic import command
from alembic.config import Config
from pydantic import PostgresDsn, TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import Environment

RUNTIME_ROLE: Final[str] = "badi_app"
MIN_APP_PASSWORD_LENGTH: Final[int] = 16
VALID_ENVIRONMENTS: Final[set[str]] = {"development", "test"}
POSTGRES_DSN_ADAPTER: Final[TypeAdapter[PostgresDsn]] = TypeAdapter(PostgresDsn)


class MigrationConfigError(ValueError):
    """Safe configuration error whose message contains no secret material."""


def validate_environment(value: str | None) -> Environment:
    environment = value or "development"
    if environment not in VALID_ENVIRONMENTS:
        raise MigrationConfigError("app.migrate only runs in development")
    return cast(Environment, environment)


def validate_asyncpg_dsn(value: str | None, *, variable: str) -> str:
    if not value:
        raise MigrationConfigError(f"{variable} is required")
    try:
        parsed = POSTGRES_DSN_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise MigrationConfigError(f"{variable} must be a valid PostgreSQL async DSN") from exc
    if parsed.scheme != "postgresql+asyncpg":
        raise MigrationConfigError(f"{variable} must use postgresql+asyncpg://")
    return str(parsed).rstrip("/")


def validate_app_password(value: str | None) -> str:
    if value is None:
        raise MigrationConfigError("BADI_APP_DB_PASSWORD is required")
    if len(value) < MIN_APP_PASSWORD_LENGTH:
        raise MigrationConfigError(
            f"BADI_APP_DB_PASSWORD must be at least {MIN_APP_PASSWORD_LENGTH} characters"
        )
    return value


@contextmanager
def scoped_database_url(database_url: str) -> Iterator[None]:
    previous = os.environ.get("BADI_DATABASE_URL")
    os.environ["BADI_DATABASE_URL"] = database_url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("BADI_DATABASE_URL", None)
        else:
            os.environ["BADI_DATABASE_URL"] = previous


def alembic_config() -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "migrations"))
    return config


def run_alembic_upgrade(database_url: str) -> None:
    with scoped_database_url(database_url):
        command.upgrade(alembic_config(), "head")


async def role_exists(connection: AsyncConnection) -> bool:
    result = await connection.scalar(
        text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
        {"role_name": RUNTIME_ROLE},
    )
    return bool(result)


async def create_runtime_role_if_missing(connection: AsyncConnection, password: str) -> None:
    if await role_exists(connection):
        return
    ddl = await connection.scalar(
        text(
            "SELECT format("
            "'CREATE ROLE badi_app LOGIN PASSWORD %s "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', "
            "format('%L', CAST(:password AS text)))"
        ),
        {"password": password},
    )
    if not isinstance(ddl, str):
        raise RuntimeError("failed to prepare runtime role DDL")
    await connection.exec_driver_sql(ddl)


async def grant_runtime_privileges(engine: AsyncEngine, password: str) -> None:
    async with engine.begin() as connection:
        await create_runtime_role_if_missing(connection, password)
        unsafe_role = await connection.scalar(
            text(
                "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
                "OR EXISTS (SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid) "
                "FROM pg_roles WHERE rolname = :role"
            ),
            {"role": RUNTIME_ROLE},
        )
        if unsafe_role is not False:
            raise RuntimeError("existing runtime role has unsafe privileges")
        grant_connect = await connection.scalar(
            text("SELECT format('GRANT CONNECT ON DATABASE %I TO badi_app', current_database())")
        )
        if not isinstance(grant_connect, str):
            raise RuntimeError("failed to prepare database grant")
        await connection.exec_driver_sql(grant_connect)
        await connection.execute(text("GRANT USAGE ON SCHEMA public TO badi_app"))
        await connection.execute(text("GRANT SELECT ON TABLE alembic_version TO badi_app"))
        await connection.execute(text("GRANT SELECT, INSERT, UPDATE ON TABLE users TO badi_app"))
        await connection.execute(text("GRANT SELECT, INSERT, UPDATE ON TABLE tickets TO badi_app"))
        await connection.execute(text("GRANT SELECT, INSERT ON TABLE ticket_messages TO badi_app"))
        await connection.execute(text("GRANT SELECT, INSERT ON TABLE ticket_events TO badi_app"))
        await connection.execute(
            text("GRANT SELECT, INSERT, UPDATE ON TABLE outbox_events TO badi_app")
        )
        await connection.execute(
            text("GRANT SELECT, INSERT, UPDATE ON TABLE ticket_sla_cycles TO badi_app")
        )
        await connection.execute(
            text("GRANT SELECT, INSERT, UPDATE ON TABLE saved_replies TO badi_app")
        )
        await connection.execute(
            text("GRANT SELECT, INSERT, UPDATE ON TABLE user_ticket_state TO badi_app")
        )
        await connection.execute(
            text("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE attachments TO badi_app")
        )
        await connection.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE notification_deliveries TO badi_app"
            )
        )
        await connection.execute(
            text("GRANT SELECT, INSERT, UPDATE ON TABLE notification_preferences TO badi_app")
        )
        await connection.execute(
            text("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE idempotency_records TO badi_app")
        )
        await connection.execute(
            text("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE auth_sessions TO badi_app")
        )
        await connection.execute(
            text("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE auth_throttles TO badi_app")
        )
        await connection.execute(
            text("GRANT USAGE, SELECT ON SEQUENCE ticket_sequence_seq TO badi_app")
        )
        can_create = await connection.scalar(
            text("SELECT has_schema_privilege('badi_app', 'public', 'CREATE')")
        )
        if can_create is not False:
            raise RuntimeError("runtime role must not own or create application schema objects")


async def provision_runtime_role(database_url: str, password: str) -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        await grant_runtime_privileges(engine, password)
    finally:
        await engine.dispose()


async def run(
    environ: dict[str, str],
    *,
    upgrade: Callable[[str], None] = run_alembic_upgrade,
    provision: Callable[[str, str], Awaitable[None] | None] = provision_runtime_role,
) -> int:
    try:
        validate_environment(environ.get("BADI_ENVIRONMENT"))
        migration_database_url = validate_asyncpg_dsn(
            environ.get("BADI_MIGRATION_DATABASE_URL"), variable="BADI_MIGRATION_DATABASE_URL"
        )
        app_password = validate_app_password(environ.get("BADI_APP_DB_PASSWORD"))
    except MigrationConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        # Alembic's async env owns its event loop. Do not nest asyncio.run inside
        # this CLI's loop; the migration finishes before runtime-role grants.
        await asyncio.to_thread(upgrade, migration_database_url)
        maybe_awaitable = provision(migration_database_url, app_password)
        if maybe_awaitable is not None:
            await maybe_awaitable
    except Exception:
        print("migration failed; check database credentials and server logs", file=sys.stderr)
        return 1

    print("development database migrated and runtime role grants verified")
    return 0


def main(_argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(dict(os.environ)))


if __name__ == "__main__":
    raise SystemExit(main())
