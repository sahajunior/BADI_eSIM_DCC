"""Attachment content validation and scanning.

Signature detection does not trust the browser Content-Type. Scanning is
fail-closed: if it is not explicitly enabled, uploads are refused rather than
accepted unchecked. The dev-only bypass is labelled and never the default.
"""

from __future__ import annotations

from app.config import Settings

ALLOWED_TYPES = {"image/png", "image/jpeg", "application/pdf"}
EXTENSIONS = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "application/pdf": {".pdf"},
}
_MAX_FILENAME = 255
_DANGEROUS_MARKERS = (b"<script", b"<svg", b"<!doctype html", b"<html", b"<?php", b"MZ")


class ScannerUnavailable(RuntimeError):
    """Raised when scanning is disabled; callers must fail closed."""


class ScanRejected(ValueError):
    """Raised when the content fails validation or scanning."""


def sanitize_filename(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1].strip()
    base = "".join(ch for ch in base if ch.isprintable() and ch not in {'"', "\r", "\n"})
    if not base:
        base = "attachment"
    return base[:_MAX_FILENAME]


def detect_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def scan(data: bytes, filename: str, settings: Settings) -> str:
    """Return the detected media type, or raise ScanRejected/ScannerUnavailable."""
    if settings.attachment_scanning != "enabled":
        # Fail closed. A development-only bypass must be explicit and labelled.
        raise ScannerUnavailable("attachment scanning is disabled")

    detected = detect_type(data)
    if detected is None:
        raise ScanRejected("unsupported or unrecognized file type")
    lowered = data[:4096].lower()
    if any(marker in lowered for marker in _DANGEROUS_MARKERS):
        raise ScanRejected("content signature is not allowed")
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if f".{suffix}" not in EXTENSIONS[detected]:
        raise ScanRejected("file extension does not match its content")
    return detected
