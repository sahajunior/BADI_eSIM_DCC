from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.config import Settings
from app.db import create_engine, dispose_engine
from app.models import Role, User

SUCCESS = 0
VALIDATION_ERROR = 2
DUPLICATE_ERROR = 3
DATABASE_ERROR = 4


def canonical_email(value: str) -> str:
    try:
        result = validate_email(value.strip(), check_deliverability=False, test_environment=True)
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc
    return result.normalized.lower()


def non_blank(value: str, *, field: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    if len(normalized) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create one support user without logging or accepting the password on argv.",
        epilog=(
            "Return codes: 0 created, 2 invalid input/password mismatch, "
            "3 duplicate email, 4 database error. Password is read with getpass."
        ),
    )
    parser.add_argument(
        "--email", required=True, help="User email address; stored canonical lowercase."
    )
    parser.add_argument("--name", required=True, help="Display name, 1-100 characters after trim.")
    parser.add_argument("--role", required=True, choices=[role.value for role in Role])
    return parser


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    name: str,
    role: Role,
    password: str,
) -> User:
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise ValueError(f"user already exists: {email}")

    user = User(
        email=email,
        display_name=name,
        password_hash=hash_password(password),
        role=role.value,
        is_active=True,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError(f"user already exists: {email}") from exc
    await session.refresh(user)
    return user


def read_password() -> str:
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ValueError("passwords do not match")
    if not 12 <= len(password) <= 1024:
        raise ValueError("password must be between 12 and 1024 characters")
    return password


async def run(args: argparse.Namespace) -> int:
    try:
        email = canonical_email(args.email)
        name = non_blank(args.name, field="name", max_length=100)
        role = Role(args.role)
        password = read_password()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return VALIDATION_ERROR

    settings = Settings()
    engine = create_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            user = await create_user(session, email=email, name=name, role=role, password=password)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return DUPLICATE_ERROR if "already exists" in str(exc) else VALIDATION_ERROR
    except Exception as exc:
        print(f"database error: {exc.__class__.__name__}", file=sys.stderr)
        return DATABASE_ERROR
    finally:
        await dispose_engine(engine)

    print(f"created user {user.email} ({user.role}) id={user.id}")
    return SUCCESS


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
