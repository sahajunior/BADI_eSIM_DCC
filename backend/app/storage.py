"""Private attachment object storage (local filesystem adapter).

The application authorizes every read; objects are addressed by random storage keys
and never exposed by path. This is the P1 private-storage placeholder; a deployment
would swap the adapter for a private object store with equivalent guarantees.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from app.config import Settings


def new_storage_key() -> str:
    return secrets.token_hex(32)


class LocalAttachmentStorage:
    def __init__(self, settings: Settings) -> None:
        base = Path(settings.attachment_dir)
        if not base.is_absolute():
            base = Path.cwd() / base
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not key.isalnum() or len(key) > 64:
            raise ValueError("invalid storage key")
        return self.base / key

    def save(self, key: str, data: bytes) -> None:
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def load(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass


def storage_for(settings: Settings) -> LocalAttachmentStorage:
    return LocalAttachmentStorage(settings)
