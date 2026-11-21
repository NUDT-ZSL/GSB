"""Content-addressed on-disk cache for alignment artifacts.

Every cache key is the sha256 of the canonical JSON of its components
(PCM hash, frame hop/len, seam thresholds, declared sample rates, upstream
artifact hashes, ...).  Identical keys reuse the persisted snapshot;
different keys force recomputation.  Each entry can be inspected for its
key composition and content hash.
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import ResourceLimitError
from .util import canonical_json, hash_obj


class ArtifactCache:
    def __init__(self, directory, max_entries=512):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_entries = max_entries

    @staticmethod
    def make_key(components: dict) -> str:
        return hash_obj({"cache_key": components})

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def _entry_count(self) -> int:
        return sum(1 for p in self.dir.glob("*.json"))

    def get(self, key: str):
        path = self._path(key)
        if not path.exists():
            return None
        entry = json.loads(path.read_text())
        if entry["content_hash"] != hash_obj(entry["payload"]):
            return None  # corrupted entry: force recompute
        return entry["payload"]

    def put(self, components: dict, payload) -> str:
        key = self.make_key(components)
        path = self._path(key)
        if not path.exists() and self._entry_count() >= self.max_entries:
            raise ResourceLimitError(
                "cache entry limit exceeded",
                limit="max_cache_entries",
                max_entries=self.max_entries,
            )
        entry = {
            "key": key,
            "key_components": components,
            "content_hash": hash_obj(payload),
            "payload": payload,
        }
        path.write_text(canonical_json(entry))
        return key

    def describe(self, key: str):
        """Return key composition + content hash for a given key."""
        path = self._path(key)
        if not path.exists():
            return None
        entry = json.loads(path.read_text())
        return {
            "key": entry["key"],
            "key_components": entry["key_components"],
            "content_hash": entry["content_hash"],
        }
