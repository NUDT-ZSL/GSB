"""Versioned target-side prefix cache with integrity checks."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterator

from .errors import E_CACHE_CORRUPT, E_RESOURCE_LIMIT, fail
from .models import DecoderState, StepRecord
from .util import sha256_value


def prefix_cache_key(metadata: dict[str, Any]) -> str:
    required = (
        "source_encoding_digest",
        "align_version",
        "align_checksum",
        "block_size",
        "context_window",
        "upstream_node_hash",
        "boundary_step",
        "config_checksum",
        "snapshot_fingerprint",
    )
    missing = [field for field in required if field not in metadata]
    if missing:
        raise ValueError(f"prefix cache key metadata missing: {missing}")
    return sha256_value({"prefix-cache-key-v1": metadata})


@dataclass(frozen=True)
class PrefixCacheEntry:
    key: str
    boundary_step: int
    metadata: dict[str, Any]
    payload: dict[str, Any]
    payload_hash: str
    version: int

    def semantic_hash(self) -> str:
        return sha256_value(self.payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "boundary_step": self.boundary_step,
            "metadata": copy.deepcopy(self.metadata),
            "payload": copy.deepcopy(self.payload),
            "payload_hash": self.payload_hash,
            "version": self.version,
        }

    def state(self) -> DecoderState:
        return DecoderState.from_dict(self.payload["state"])


class PrefixCacheStore:
    def __init__(self, max_entries: int) -> None:
        self.max_entries = int(max_entries)
        self._entries: dict[str, PrefixCacheEntry] = {}
        self._lock = threading.RLock()

    @staticmethod
    def make_entry(
        metadata: dict[str, Any],
        state: DecoderState,
        *,
        version: int,
        record: StepRecord | None = None,
    ) -> PrefixCacheEntry:
        key = prefix_cache_key(metadata)
        payload: dict[str, Any] = {"state": state.to_dict()}
        if record is not None:
            payload["record"] = record.to_dict(include_candidates=True)
        return PrefixCacheEntry(
            key=key,
            boundary_step=int(metadata["boundary_step"]),
            metadata=copy.deepcopy(metadata),
            payload=payload,
            payload_hash=sha256_value(payload),
            version=int(version),
        )

    def _verify(self, entry: PrefixCacheEntry) -> PrefixCacheEntry:
        expected_key = prefix_cache_key(entry.metadata)
        if expected_key != entry.key:
            fail(
                E_CACHE_CORRUPT,
                "prefix cache key does not match its metadata",
                field="prefix_cache",
                data={
                    "entry_key": entry.key,
                    "version": entry.version,
                    "expected_key": expected_key,
                    "category": "key",
                },
            )
        actual_hash = entry.semantic_hash()
        if actual_hash != entry.payload_hash:
            fail(
                E_CACHE_CORRUPT,
                "prefix cache payload checksum verification failed",
                field="prefix_cache",
                data={
                    "entry_key": entry.key,
                    "version": entry.version,
                    "expected_hash": entry.payload_hash,
                    "actual_hash": actual_hash,
                    "category": "payload",
                },
            )
        try:
            state = entry.state()
        except (KeyError, TypeError, ValueError) as exc:
            fail(
                E_CACHE_CORRUPT,
                "prefix cache state cannot be decoded",
                field="prefix_cache",
                data={
                    "entry_key": entry.key,
                    "version": entry.version,
                    "category": "payload",
                    "reason": str(exc),
                },
            )
        if state.state_hash != entry.payload["state"].get("state_hash"):
            fail(
                E_CACHE_CORRUPT,
                "prefix cache state hash verification failed",
                field="prefix_cache",
                data={
                    "entry_key": entry.key,
                    "version": entry.version,
                    "category": "state_hash",
                },
            )
        return entry

    def put(self, entry: PrefixCacheEntry) -> PrefixCacheEntry:
        self._verify(entry)
        with self._lock:
            existing = self._entries.get(entry.key)
            if existing is not None:
                return self._verify(existing)
            if len(self._entries) >= self.max_entries:
                fail(
                    E_RESOURCE_LIMIT,
                    "prefix cache entry limit exceeded",
                    field="prefix_cache_entries",
                    data={
                        "current": len(self._entries),
                        "limit": self.max_entries,
                        "attempted_entry_key": entry.key,
                    },
                )
            self._entries[entry.key] = entry
            return entry

    def get(self, key: str) -> PrefixCacheEntry:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                fail(
                    E_CACHE_CORRUPT,
                    "prefix cache entry is missing",
                    field="prefix_cache",
                    data={
                        "entry_key": key,
                        "version": 0,
                        "category": "missing",
                    },
                )
            return self._verify(entry)

    def contains(self, key: str) -> bool:
        with self._lock:
            return key in self._entries

    def delete(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._entries)

    def items(self) -> Iterator[tuple[str, PrefixCacheEntry]]:
        with self._lock:
            snapshot = tuple(self._entries.items())
        return iter(snapshot)

    def entries(self) -> tuple[PrefixCacheEntry, ...]:
        with self._lock:
            return tuple(self._entries.values())

    def verify_all(self) -> dict[str, Any]:
        entries = self.entries()
        for entry in entries:
            self._verify(entry)
        return {
            "entry_count": len(entries),
            "keys": [entry.key for entry in entries],
            "all_valid": True,
        }

    def tamper_entry(
        self,
        key: str,
        mutator: Callable[[dict[str, Any]], None],
    ) -> None:
        """Diagnostics only: intentionally leave checksums stale."""

        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                fail(
                    E_CACHE_CORRUPT,
                    "prefix cache entry is missing",
                    field="prefix_cache",
                    data={"entry_key": key, "version": 0, "category": "missing"},
                )
            payload = copy.deepcopy(entry.payload)
            mutator(payload)
            self._entries[key] = replace(entry, payload=payload)

    def find_by_boundary(self, boundary_step: int) -> PrefixCacheEntry | None:
        for entry in self.entries():
            if entry.boundary_step == boundary_step:
                return self._verify(entry)
        return None

    def max_boundary_le(self, boundary_step: int) -> PrefixCacheEntry | None:
        candidates = [
            entry
            for entry in self.entries()
            if entry.boundary_step <= boundary_step
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda entry: entry.boundary_step)

