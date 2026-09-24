import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from urllib.parse import urlparse

from fastapi import Request, Response
from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import ApiError
from app.models import AuthThrottle

SESSION_COOKIE_NAME = "badi_session"
CSRF_HEADER_NAME = "x-csrf-token"
BOOTSTRAP_LIMIT = 60
LOGIN_IP_LIMIT = 20
LOGIN_EMAIL_LIMIT = 5
THROTTLE_WINDOW_SECONDS = 300


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_session_token() -> str:
    return token_urlsafe(32)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_csrf_token(settings: Settings, raw_session_token: str) -> str:
    secret = settings.auth_secret.get_secret_value().encode("utf-8")
    return hmac.new(secret, raw_session_token.encode("utf-8"), hashlib.sha256).hexdigest()


def csrf_matches(settings: Settings, raw_session_token: str, supplied_token: str | None) -> bool:
    if supplied_token is None or len(supplied_token) != 64 or not supplied_token.isascii():
        return False
    return hmac.compare_digest(make_csrf_token(settings, raw_session_token), supplied_token)


def allowed_origin_set(settings: Settings) -> set[str]:
    return set(settings.allowed_origins)


def request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return origin
    referer = request.headers.get("referer")
    if not referer:
        return None
    try:
        parsed = urlparse(referer)
    except ValueError:
        return None
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def enforce_get_csrf_fetch_policy(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin")
    if origin and origin not in allowed_origin_set(settings):
        raise ApiError(403, "forbidden_origin", "Origin is not allowed.")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise ApiError(403, "cross_site", "Cross-site requests are not allowed.")


def enforce_post_origin(request: Request, settings: Settings) -> None:
    origin = request_origin(request)
    if origin not in allowed_origin_set(settings):
        raise ApiError(403, "forbidden_origin", "Origin is not allowed.")


def client_ip(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host


def throttle_key_hash(purpose: str, value: str) -> str:
    return sha256_hex(f"{purpose}:{value.strip().lower()}")


async def hit_throttle(
    db: AsyncSession,
    *,
    purpose: str,
    value: str,
    limit: int,
    window_seconds: int = THROTTLE_WINDOW_SECONDS,
) -> None:
    now = utcnow()
    key_hash = throttle_key_hash(purpose, value)
    insert_statement = insert(AuthThrottle).values(
        key_hash=key_hash,
        window_started_at=now,
        attempts=1,
    )
    stale = AuthThrottle.window_started_at < now - timedelta(seconds=window_seconds)
    # SQLAlchemy case expressions are clearer when assigned after insert construction.
    statement = insert_statement.on_conflict_do_update(
        index_elements=[AuthThrottle.key_hash],
        set_={
            "window_started_at": case((stale, now), else_=AuthThrottle.window_started_at),
            "attempts": case((stale, 1), else_=AuthThrottle.attempts + 1),
        },
    ).returning(AuthThrottle.window_started_at, AuthThrottle.attempts)
    result = await db.execute(statement)
    row = result.one()
    await db.commit()
    attempts = int(row.attempts)
    if attempts > limit:
        raise ApiError(429, "rate_limited", "Too many attempts. Try again later.")


def session_expires_at(settings: Settings, *, authenticated: bool) -> datetime:
    ttl = settings.session_ttl_seconds if authenticated else settings.preauth_ttl_seconds
    return utcnow() + timedelta(seconds=ttl)


def set_session_cookie(
    response: Response, settings: Settings, raw_token: str, *, authenticated: bool
) -> None:
    max_age = settings.session_ttl_seconds if authenticated else settings.preauth_ttl_seconds
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
