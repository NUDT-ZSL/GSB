"""Local content-addressed artifact cache for computation graph nodes."""

from __future__ import annotations

import os
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .serialization import canonical_bytes, canonical_text, clone_json, sha256_bytes


@dataclass(frozen=True)
class CacheEntry:
    key: str
    node_id: str
    node_type: str
    content_hash: str
    depends_on: tuple[str, ...]
    payload: dict[str, Any]
    key_material: dict[str, Any] | None = None

    def as_dict(self, *, include_payload: bool = False) -> dict[str, Any]:
        result = {
            "key": self.key,
            "node_id": self.node_id,
            "node_type": self.node_type,
            "content_hash": self.content_hash,
            "depends_on": list(self.depends_on),
        }
        if include_payload:
            result["payload"] = clone_json(self.payload)
        if self.key_material:
            result["key_material"] = clone_json(self.key_material)
        return result


class ArtifactCache:
    """A crash-safe, file-backed cache keyed by canonical node material."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def path_for_key(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> CacheEntry | None:
        path = self.path_for_key(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                code=ErrorCode.CACHE_CORRUPT,
                message=f"cache entry {key} cannot be decoded: {exc}",
                stage="output",
                field="cache",
                value=key,
            ) from exc
        payload = data.get("payload")
        stored_hash = str(data.get("content_hash", ""))
        actual_hash = sha256_bytes(canonical_bytes(payload))
        if stored_hash != actual_hash:
            raise BusinessError(
                code=ErrorCode.CACHE_CORRUPT,
                message=f"cache entry {key} content hash mismatch",
                stage="output",
                field="content_hash",
                value={"stored": stored_hash, "actual": actual_hash},
            )
        if str(data.get("key")) != key:
            raise BusinessError(
                code=ErrorCode.CACHE_CORRUPT,
                message=f"cache entry {key} key mismatch",
                stage="output",
                field="key",
                value=data.get("key"),
            )
        self.hits += 1
        return CacheEntry(
            key=key,
            node_id=str(data.get("node_id", "")),
            node_type=str(data.get("node_type", "")),
            content_hash=stored_hash,
            depends_on=tuple(str(item) for item in data.get("depends_on", [])),
            payload=clone_json(payload),
            key_material=clone_json(data.get("key_material", {})),
        )

    def put(
        self,
        *,
        key: str,
        node_id: str,
        node_type: str,
        payload: dict[str, Any],
        depends_on: list[str] | tuple[str, ...] = (),
        key_material: dict[str, Any] | None = None,
    ) -> CacheEntry:
        payload_bytes = canonical_bytes(payload)
        content_digest = sha256_bytes(payload_bytes)
        record = {
            "cache_format": 1,
            "key": key,
            "node_id": node_id,
            "node_type": node_type,
            "content_hash": content_digest,
            "depends_on": list(depends_on),
            "key_material": clone_json(key_material or {}),
            "payload": clone_json(payload),
        }
        path = self.path_for_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{key}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(canonical_bytes(record))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return CacheEntry(
            key=key,
            node_id=node_id,
            node_type=node_type,
            content_hash=content_digest,
            depends_on=tuple(depends_on),
            payload=clone_json(payload),
            key_material=clone_json(key_material or {}),
        )

    def query(self, key: str) -> dict[str, Any] | None:
        entry = self.get(key)
        return None if entry is None else entry.as_dict(include_payload=True)

    def list_entries(self, *, limit: int = 100) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            entries.append(
                {
                    "key": data.get("key"),
                    "node_id": data.get("node_id"),
                    "node_type": data.get("node_type"),
                    "content_hash": data.get("content_hash"),
                    "depends_on": data.get("depends_on", []),
                    "key_material": data.get("key_material", {}),
                }
            )
            if len(entries) >= limit:
                break
        return entries

    def stats(self) -> dict[str, int]:
        files = list(self.root.glob("*.json"))
        return {
            "entries": len(files),
            "session_hits": self.hits,
            "session_misses": self.misses,
        }


def cache_key(material: dict[str, Any]) -> str:
    return sha256_bytes(canonical_bytes(material))


def entry_debug_text(entry: CacheEntry) -> str:
    return canonical_text(entry.as_dict(include_payload=True))

