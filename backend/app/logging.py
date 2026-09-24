"""Dependency-free structured logging.

Records are JSON lines with a bounded set of operational fields. Message bodies,
credentials, cookies, and tokens are never logged by application code; only the
request path (without its query string) is included.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_EXTRA_FIELDS = (
    "request_id",
    "method",
    "path",
    "status",
    "duration_ms",
    "pending",
    "oldest_seconds",
    "event_id",
    "users_created",
    "tickets_created",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str, sort_keys=True)


def configure_logging(level: str = "INFO") -> None:
    """Install a single JSON stdout handler; safe to call more than once."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    if any(getattr(handler, "_badi_json", False) for handler in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._badi_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)
