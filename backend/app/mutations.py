"""Shared authenticated write boundary and transaction-scoped retry keys."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import Cookie, Depends, Header, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, get_db
from app.auth.security import SESSION_COOKIE_NAME, csrf_matches, enforce_post_origin
from app.errors import ApiError
from app.models import IdempotencyRecord, User


async def mutation_user(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    badi_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> User:
    settings = request.app.state.settings
    enforce_post_origin(request, settings)
    if not badi_session or not csrf_matches(settings, badi_session, csrf_token):
        raise ApiError(403, "invalid_csrf", "CSRF validation failed.")
    return user


Db = Annotated[AsyncSession, Depends(get_db)]
Writer = Annotated[User, Depends(mutation_user)]


def request_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def begin_idempotency(
    db: AsyncSession,
    actor_id: UUID,
    route: str,
    key: str | None,
    payload: dict[str, Any],
) -> IdempotencyRecord | None:
    if key is None or not 1 <= len(key) <= 128 or not key.isascii() or not key.isprintable():
        raise ApiError(
            422, "invalid_idempotency_key", "Idempotency-Key (1-128 ASCII characters) required."
        )
    # The lock covers the absent-row case too. Always acquire this before a ticket
    # row lock; no committed claim or duplicate response body is stored separately.
    lock_material = json.dumps([str(actor_id), route, key]).encode()
    lock_id = int.from_bytes(hashlib.sha256(lock_material).digest()[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
    record = await db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.route == route,
            IdempotencyRecord.key == key,
        )
    )
    if record is not None and record.expires_at <= datetime.now(UTC):
        await db.delete(record)
        await db.flush()
        record = None
    if record is not None and record.request_hash != request_digest(payload):
        raise ApiError(
            409, "idempotency_conflict", "Idempotency key already used for different content."
        )
    return record


def finish_idempotency(
    db: AsyncSession,
    actor_id: UUID,
    route: str,
    key: str,
    payload: dict[str, Any],
    result_type: str,
    result_id: UUID,
) -> None:
    db.add(
        IdempotencyRecord(
            actor_id=actor_id,
            route=route,
            key=key,
            request_hash=request_digest(payload),
            result_type=result_type,
            result_id=result_id,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
    )
