"""Content-addressed cache for normalized events, evidence, and chain states."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .canonical import canonical_json, content_hash, sha256_text


class CacheStore:
    """A small deterministic CAS.

    Corrupt cache entries are treated as misses and rewritten. Snapshot
    integrity is handled separately by :mod:`coref_pipeline.snapshot`, where
    corruption is a business error.
    """

    def __init__(self, root: str | Path | None):
        self.root = Path(root) if root is not None else None
        self.enabled = self.root is not None
        self.hits: dict[str, list[str]] = {}
        self.misses: dict[str, list[str]] = {}
        self.writes: dict[str, int] = {}
        self.corrupt: dict[str, list[str]] = {}

    def _record(self, table: dict[str, list[str]], kind: str, key: str) -> None:
        table.setdefault(kind, []).append(key)

    def _path(self, kind: str, key: str) -> Path:
        assert self.root is not None
        return self.root / kind / f"{key}.json"

    def get(self, kind: str, key: str) -> Any | None:
        if not self.enabled:
            self._record(self.misses, kind, key)
            return None
        path = self._path(kind, key)
        if not path.is_file():
            self._record(self.misses, kind, key)
            return None
        try:
            wrapper = json.loads(path.read_text(encoding="utf-8"))
            checksum = wrapper.get("checksum")
            payload = wrapper.get("payload")
            expected = content_hash({"kind": wrapper.get("kind"), "payload": payload})
            if (
                wrapper.get("kind") != kind
                or wrapper.get("key") != key
                or checksum != expected
            ):
                raise ValueError("cache checksum mismatch")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._record(self.corrupt, kind, key)
            self._record(self.misses, kind, key)
            return None
        self._record(self.hits, kind, key)
        return payload

    def put(self, kind: str, key: str, payload: Any) -> None:
        if not self.enabled:
            return
        path = self._path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        wrapper = {
            "kind": kind,
            "key": key,
            "checksum": content_hash({"kind": kind, "payload": payload}),
            "payload": payload,
        }
        data = (canonical_json(wrapper) + "\n").encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=key + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        self.writes[kind] = self.writes.get(kind, 0) + 1

    def summary(self, *, include_keys: bool = False) -> dict[str, Any]:
        kinds = sorted(set(self.hits) | set(self.misses) | set(self.writes) | set(self.corrupt))
        result: dict[str, Any] = {}
        for kind in kinds:
            entry: dict[str, Any] = {
                "hits": len(self.hits.get(kind, [])),
                "misses": len(self.misses.get(kind, [])),
                "writes": self.writes.get(kind, 0),
                "corrupt": len(self.corrupt.get(kind, [])),
            }
            if include_keys:
                entry["hit_keys"] = sorted(self.hits.get(kind, []))
                entry["miss_keys"] = sorted(self.misses.get(kind, []))
                entry["corrupt_keys"] = sorted(self.corrupt.get(kind, []))
            result[kind] = entry
        return result

    def reset_trace(self) -> None:
        self.hits.clear()
        self.misses.clear()
        self.writes.clear()
        self.corrupt.clear()


def cache_key(namespace: str, value: Any) -> str:
    return sha256_text(namespace + "\n" + canonical_json(value))

