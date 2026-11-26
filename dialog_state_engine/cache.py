from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .canonical import (
    atomic_write_json,
    content_hash,
)
from .errors import E_FORMAT_CORRUPT, fail


CACHE_SCHEMA = "content-cache-v1"


@dataclass(frozen=True)
class UpstreamRef:
    key: str
    payload_hash: str

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "payload_hash": self.payload_hash}


@dataclass(frozen=True)
class CacheObject:
    key: str
    node: str
    payload: Any
    payload_hash: str


def make_cache_key(
    node: str,
    *,
    config_digest: str,
    upstream: Iterable[UpstreamRef] = (),
    extra: Any = None,
) -> str:
    material = {
        "schema": CACHE_SCHEMA,
        "node": node,
        "config_digest": config_digest,
        "upstream": [item.to_dict() for item in upstream],
        "extra": extra,
    }
    return content_hash(material)


class ContentCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not key.startswith("sha256:") or len(key) != 71:
            fail(
                E_FORMAT_CORRUPT,
                "cache key must be a sha256 content key",
                field="cache.key",
            )
        digest = key.removeprefix("sha256:")
        if any(character not in "0123456789abcdef" for character in digest):
            fail(
                E_FORMAT_CORRUPT,
                "cache key contains non-hex characters",
                field="cache.key",
            )
        return self.objects / digest[:2] / f"{digest}.json"

    def get(self, key: str, *, node: str) -> CacheObject | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                envelope = json.load(handle)
        except (OSError, ValueError) as exc:
            fail(
                E_FORMAT_CORRUPT,
                f"cache object cannot be parsed: {exc}",
                field="cache.object",
                details={"key": key},
            )
        if not isinstance(envelope, dict):
            fail(E_FORMAT_CORRUPT, "cache object must be an object", field="cache.object")
        if envelope.get("cache_schema") != CACHE_SCHEMA:
            fail(
                E_FORMAT_CORRUPT,
                "cache schema mismatch",
                field="cache.cache_schema",
                details={"key": key},
            )
        if envelope.get("key") != key:
            fail(
                E_FORMAT_CORRUPT,
                "cache envelope key mismatch",
                field="cache.key",
                details={"expected": key, "actual": envelope.get("key")},
            )
        if envelope.get("node") != node:
            fail(
                E_FORMAT_CORRUPT,
                "cache node mismatch",
                field="cache.node",
                details={"expected": node, "actual": envelope.get("node")},
            )
        payload = envelope.get("payload")
        payload_hash = content_hash(payload)
        if envelope.get("payload_hash") != payload_hash:
            fail(
                E_FORMAT_CORRUPT,
                "cache payload hash mismatch",
                field="cache.payload_hash",
                details={"key": key},
            )
        return CacheObject(key=key, node=node, payload=payload, payload_hash=payload_hash)

    def put(self, key: str, *, node: str, payload: Any) -> CacheObject:
        path = self._path(key)
        payload_hash = content_hash(payload)
        existing = self.get(key, node=node)
        if existing is not None:
            if existing.payload_hash != payload_hash:
                fail(
                    E_FORMAT_CORRUPT,
                    "content key collision or inconsistent cache object",
                    field="cache.payload",
                    details={"key": key},
                )
            return existing
        envelope = {
            "cache_schema": CACHE_SCHEMA,
            "key": key,
            "node": node,
            "payload_hash": payload_hash,
            "payload": payload,
        }
        atomic_write_json(path, envelope)
        return CacheObject(
            key=key,
            node=node,
            payload=payload,
            payload_hash=payload_hash,
        )


