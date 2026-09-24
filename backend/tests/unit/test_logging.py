import json
import logging

from app.logging import JsonFormatter, configure_logging


def make_record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord("app.request", logging.INFO, __file__, 1, "request", None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_only_allowlisted_operational_fields() -> None:
    record = make_record(
        request_id="req-1",
        method="GET",
        path="/api/v1/tickets",
        status=200,
        duration_ms=1.25,
        body="secret ticket body",
        authorization="Bearer leak-me",
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "request"
    assert payload["request_id"] == "req-1"
    assert payload["method"] == "GET"
    assert payload["path"] == "/api/v1/tickets"
    assert payload["status"] == 200
    assert payload["duration_ms"] == 1.25
    rendered = json.dumps(payload)
    assert "secret" not in rendered
    assert "leak-me" not in rendered


def test_configure_logging_installs_one_handler_and_is_idempotent() -> None:
    root = logging.getLogger()
    original = list(root.handlers)
    try:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        configure_logging("WARNING")
        configure_logging("INFO")
        json_handlers = [h for h in root.handlers if getattr(h, "_badi_json", False)]
        assert len(json_handlers) == 1
        assert isinstance(json_handlers[0].formatter, JsonFormatter)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in original:
            root.addHandler(handler)
