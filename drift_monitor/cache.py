"""Content-addressed disk cache for per-feature statistics and test results."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .errors import DriftError, ErrorCode
from .hashing import content_hash

CACHE_KEY_VERSION = "cache-key-v1"


@dataclass
class CacheKeyComponents:
    window_sample_hash: str
    methods: list
    thresholds: dict
    stat_schema_version: str
    feature_definition_digest: str
    upstream_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key_version": CACHE_KEY_VERSION,
            "window_sample_hash": self.window_sample_hash,
            "methods": list(self.methods),
            "thresholds": dict(sorted(self.thresholds.items())),
            "stat_schema_version": self.stat_schema_version,
            "feature_definition_digest": self.feature_definition_digest,
            "upstream_hash": self.upstream_hash,
        }


def build_cache_key(components: CacheKeyComponents) -> str:
    payload = components.to_dict()
    return content_hash(payload)


def parse_cache_key(key: str) -> Dict[str, Any]:
    """A key is opaque externally; only keys produced by this module resolve."""
    if not isinstance(key, str) or len(key) != 64:
        raise DriftError(
            ErrorCode.CACHE_KEY_INVALID,
            "cache key must be a 64 character SHA-256 digest",
            {"field": "cache_key"},
        )
    try:
        int(key, 16)
    except ValueError:
        raise DriftError(
            ErrorCode.CACHE_KEY_INVALID,
            "cache key is not a hexadecimal digest",
            {"field": "cache_key"},
        )
    return {"cache_key": key, "key_version": CACHE_KEY_VERSION}


class ContentStore:
    """File-backed content store with hit accounting and entry limits."""

    def __init__(self, directory: str, max_entries: Optional[int] = None):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self.written_keys = []

    def _path(self, key: str) -> str:
        parse_cache_key(key)
        return os.path.join(self.directory, f"{key}.json")

    def has(self, key: str) -> bool:
        return os.path.exists(self._path(key))

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        path = self._path(key)
        if not os.path.exists(path):
            self.misses += 1
            return None
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        if document.get("content_hash") != content_hash(document["payload"]):
            raise DriftError(
                ErrorCode.CACHE_KEY_INVALID,
                "stored artifact failed its content checksum",
                {"field": "content_hash", "cache_key": key},
            )
        self.hits += 1
        return document

    def put(self, key: str, components: CacheKeyComponents, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.max_entries is not None:
            existing = [name for name in os.listdir(self.directory) if name.endswith(".json")]
            if len(existing) >= self.max_entries and not os.path.exists(self._path(key)):
                raise DriftError(
                    ErrorCode.RESOURCE_LIMIT,
                    "cache entry limit reached",
                    {
                        "field": "cache_entries",
                        "limit": self.max_entries,
                        "current": len(existing),
                        "degradation": "cache_write_rejected",
                    },
                )
        expected = build_cache_key(components)
        if expected != key:
            raise DriftError(
                ErrorCode.CACHE_KEY_INVALID,
                "cache key does not match its declared components",
                {"field": "cache_key"},
            )
        document = {
            "cache_key": key,
            "key_components": components.to_dict(),
            "payload": payload,
            "content_hash": content_hash(payload),
        }
        with open(self._path(key), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, sort_keys=True, indent=2, ensure_ascii=False)
            handle.write("\n")
        if key not in self.written_keys:
            self.written_keys.append(key)
        return document

    def replay(self, key: str) -> Dict[str, Any]:
        document = self.get(key)
        if document is None:
            raise DriftError(
                ErrorCode.CACHE_KEY_INVALID,
                "no artifact is stored under that cache key",
                {"field": "cache_key", "cache_key": key},
            )
        return document

    def entry_count(self) -> int:
        return len([name for name in os.listdir(self.directory) if name.endswith(".json")])
