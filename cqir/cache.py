from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .errors import fail
from .util import atomic_write_text, canonical_json, sha256_text


class ContentAddressedCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._memory: dict[str, Any] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(*parts: Any) -> str:
        return sha256_text(canonical_json(list(parts)))

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def put(self, key: str, payload: Any) -> Path:
        body = canonical_json({"key": key, "payload": payload})
        checksum = sha256_text(body)
        envelope = {"key": key, "payload": payload, "sha256": checksum}
        path = self._path(key)
        if path.exists():
            existing = self.get(key)
            if existing is not None:
                return path
        atomic_write_text(path, canonical_json(envelope) + "\n")
        with self._lock:
            self._memory[key] = payload
        return path

    def get(self, key: str) -> Any | None:
        with self._lock:
            if key in self._memory:
                return self._memory[key]
        path = self._path(key)
        if not path.exists():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            body = canonical_json({"key": envelope["key"], "payload": envelope["payload"]})
            if envelope["key"] != key or envelope["sha256"] != sha256_text(body):
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "content-addressed cache checksum mismatch",
                    {"key": key, "path": str(path)},
                )
            payload = envelope["payload"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            fail(
                "E_SNAPSHOT_CORRUPT",
                "content-addressed cache entry cannot be parsed",
                {"key": key, "path": str(path), "reason": str(exc)},
            )
        with self._lock:
            self._memory[key] = payload
        return payload

    def exists(self, key: str) -> bool:
        with self._lock:
            if key in self._memory:
                return True
        return self._path(key).exists()


def make_temp_name(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
