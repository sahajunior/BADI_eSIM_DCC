import hashlib
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import Cookie, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.security import SESSION_COOKIE_NAME
from app.errors import ApiError
from app.models import AuthSession, User

SESSION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
STAFF_ROLES = {"AGENT", "ADMIN"}


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise ApiError(
            status_code=503,
            code="database_unavailable",
            message="Database session factory is not configured.",
        )
    factory = cast(async_sessionmaker[AsyncSession], session_factory)
    async with factory() as session:
        yield session


def hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def is_valid_session_token(raw_token: str | None) -> bool:
    return bool(raw_token and SESSION_TOKEN_PATTERN.fullmatch(raw_token))


async def load_session(
    db: AsyncSession,
    raw_token: str | None,
    *,
    require_user: bool = False,
) -> AuthSession | None:
    if not is_valid_session_token(raw_token):
        return None
    now = datetime.now(UTC)
    statement = select(AuthSession).where(
        AuthSession.token_hash == hash_session_token(raw_token or ""),
        AuthSession.expires_at > now,
        AuthSession.revoked_at.is_(None),
    )
    if require_user:
        statement = statement.where(AuthSession.user_id.is_not(None))
    result = await db.execute(statement)
    return result.scalar_one_or_none()


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    badi_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> User:
    session = await load_session(db, badi_session, require_user=True)
    if session is None or session.user_id is None:
        raise ApiError(status_code=401, code="unauthenticated", message="Authentication required.")

    user = await db.get(User, session.user_id)
    if user is None or not bool(user.is_active):
        raise ApiError(status_code=401, code="unauthenticated", message="Authentication required.")
    return user


def require_staff(user: Annotated[User, Depends(get_current_user)]) -> User:
    if str(user.role) not in STAFF_ROLES:
        raise ApiError(status_code=403, code="forbidden", message="Staff access required.")
    return user


def user_identity(user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
    }
