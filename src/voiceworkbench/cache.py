"""Content-fingerprint stage cache with explicit reuse/invalidation ledgers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .determinism import derived_key, read_json, atomic_write_json

STAGES = ("features", "vad", "clustering", "assignment")


@dataclass
class CacheEntry:
    stage: str
    cache_key: str
    upstream_keys: list[str]
    params_fingerprint: str
    artifact: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "cache_key": self.cache_key,
            "upstream_keys": sorted(self.upstream_keys),
            "params_fingerprint": self.params_fingerprint,
            "artifact": self.artifact,
        }


@dataclass
class Ledger:
    reused: list[dict[str, Any]] = field(default_factory=list)
    invalidated: list[dict[str, Any]] = field(default_factory=list)
    recomputed: list[dict[str, Any]] = field(default_factory=list)

    def record_reuse(self, stage: str, cache_key: str) -> None:
        self.reused.append({"stage": stage, "cache_key": cache_key})

    def record_invalidation(self, stage: str, old_key: str, reason: str) -> None:
        self.invalidated.append({"stage": stage, "old_cache_key": old_key, "reason": reason})

    def record_recompute(self, stage: str, cache_key: str) -> None:
        self.recomputed.append({"stage": stage, "cache_key": cache_key})

    def to_dict(self) -> dict[str, Any]:
        return {
            "reused": list(self.reused),
            "invalidated": list(self.invalidated),
            "recomputed": list(self.recomputed),
            "reuse_count": len(self.reused),
            "invalidation_count": len(self.invalidated),
            "recompute_count": len(self.recomputed),
        }


class StageCache:
    """In-memory stage cache; each entry is fully determined by its cache key."""

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    def get(self, stage: str, cache_key: str) -> CacheEntry | None:
        entry = self._entries.get(cache_key)
        if entry is not None and entry.stage == stage:
            return entry
        return None

    def put(self, stage: str, cache_key: str, upstream_keys: list[str],
            params_fingerprint: str, artifact: Any,
            ledger: Ledger, reason: str = "compute") -> CacheEntry:
        previous = self._entries.get(cache_key)
        if previous is None or previous.stage != stage:
            ledger.record_recompute(stage, cache_key)
        entry = CacheEntry(
            stage=stage,
            cache_key=cache_key,
            upstream_keys=sorted(upstream_keys),
            params_fingerprint=params_fingerprint,
            artifact=artifact,
        )
        self._entries[cache_key] = entry
        return entry

    def invalidate_stage(self, stage: str, ledger: Ledger, reason: str) -> None:
        for key, entry in list(self._entries.items()):
            if entry.stage == stage:
                ledger.record_invalidation(stage, key, reason)
                del self._entries[key]

    def keys_for_stage(self, stage: str) -> list[str]:
        return sorted(key for key, entry in self._entries.items() if entry.stage == stage)

    def all_keys(self) -> list[str]:
        return sorted(self._entries)

    def persist(self, path: str) -> None:
        atomic_write_json(path, {
            "entries": [entry.to_dict() for entry in self._entries.values()]
        })

    @classmethod
    def restore(cls, path: str) -> "StageCache":
        cache = cls()
        if not path:
            return cache
        try:
            payload = read_json(path)
        except FileNotFoundError:
            return cache
        for raw in payload.get("entries", []):
            entry = CacheEntry(
                stage=raw["stage"],
                cache_key=raw["cache_key"],
                upstream_keys=list(raw["upstream_keys"]),
                params_fingerprint=raw["params_fingerprint"],
                artifact=raw["artifact"],
            )
            cache._entries[entry.cache_key] = entry
        return cache


def make_stage_key(upstream_keys: list[str], params_fingerprint: str) -> str:
    return derived_key(upstream_keys, params_fingerprint)
