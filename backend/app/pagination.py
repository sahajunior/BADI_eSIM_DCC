"""Small signed cursor helpers for bounded keyset pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from typing import Any

from app.config import Settings
from app.errors import ApiError


def _secret(settings: Settings) -> bytes:
    return settings.auth_secret.get_secret_value().encode("utf-8")


def encode_cursor(settings: Settings, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    sig = hmac.new(_secret(settings), body, hashlib.sha256).hexdigest().encode()
    return base64.urlsafe_b64encode(body + b"." + sig).decode()


def decode_cursor(
    settings: Settings, cursor: str | None, expected_filters: dict[str, Any]
) -> dict[str, Any] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        body, supplied_sig = raw.rsplit(b".", 1)
    except Exception as exc:
        raise ApiError(422, "invalid_cursor", "Invalid pagination cursor.") from exc
    expected_sig = hmac.new(_secret(settings), body, hashlib.sha256).hexdigest().encode()
    if not hmac.compare_digest(supplied_sig, expected_sig):
        raise ApiError(422, "invalid_cursor", "Invalid pagination cursor.")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ApiError(422, "invalid_cursor", "Invalid pagination cursor.") from exc
    if not isinstance(payload, dict):
        raise ApiError(422, "invalid_cursor", "Invalid pagination cursor.")
    if payload.get("filters") != expected_filters:
        raise ApiError(422, "invalid_cursor", "Cursor does not match current filters.")
    return payload


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
