"""Content-addressed local cache with validated composite keys.

Each cache entry stores a node payload together with:

* the full composite key breakdown (``key_details``) for diagnostics;
* the composite ``key`` hash;
* a content hash over the payload so keys and products can be cross-checked.

Writes are guarded by an in-process stage lock keyed by the composite key.
Concurrent writers of the same key are serialized so exactly one commit takes
effect, and a writer that arrives while the stage is busy fails immediately
with ``E_STAGE_BUSY`` instead of blocking.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from .deterministic import canonical_bytes, content_hash, hash_bytes
from .errors import ErrorCode, make_error


@dataclass(frozen=True)
class CacheEntry:
    key: str
    node: str
    key_details: Dict[str, object]
    payload: Dict[str, object]
    content_hash: str


class ContentCache:
    """File-backed content-addressed cache with strict key validation."""

    # Every node must contribute all semantic inputs listed here.  The exact
    # required set is enforced per node in :meth:`build_key`.
    GLOBAL_REQUIRED = (
        "node",
        "page_id",
        "pixel_hash",
        "config_hash",
        "upstream_hash",
    )
    NODE_REQUIRED: Dict[str, Set[str]] = {
        "preprocess": {"detect_threshold"},
        "detect": {"detect_threshold", "line_box_summary"},
        "features": {"line_box_summary", "detect_threshold"},
        "match": {"charset_version", "line_box_summary"},
        "recognize": {"charset_version", "dedup_threshold", "line_box_summary"},
        "aggregate": {"charset_version", "truth_hash", "line_box_summary"},
    }

    def __init__(self, root: str, max_entries: int) -> None:
        self.root = root
        self.entries_dir = os.path.join(root, "entries")
        self.max_entries = max_entries
        os.makedirs(self.entries_dir, exist_ok=True)
        self._stage_locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------------ keys
    def build_key(
        self, node: str, key_details: Dict[str, object]
    ) -> str:
        """Validate all required key components and return the composite key.

        Missing or empty components raise ``E_CACHE_KEY_INVALID`` naming every
        absent field, so no under-specified key can ever hit the cache.
        """

        required = set(self.GLOBAL_REQUIRED) | self.NODE_REQUIRED.get(node, set())
        missing: List[str] = []
        for field in sorted(required):
            if field not in key_details or key_details[field] in (None, "", []):
                missing.append(field)
        if missing:
            raise make_error(
                ErrorCode.CACHE_KEY_INVALID,
                "cache key is missing required components",
                stage=node,
                page_id=str(key_details.get("page_id")),
                missing_fields=missing,
            )
        return content_hash({"node": node, "details": key_details})

    def _path_for(self, key: str) -> str:
        # Shard by first two hex chars to keep directories bounded.
        shard = os.path.join(self.entries_dir, key[:2])
        os.makedirs(shard, exist_ok=True)
        return os.path.join(shard, f"{key}.json")

    # ------------------------------------------------------------------ read
    def get(self, key: str) -> Optional[CacheEntry]:
        """Return a validated entry, or ``None`` on a clean miss."""

        path = self._path_for(key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
            payload = record["payload"]
            rebuilt_payload_hash = content_hash(payload)
            if rebuilt_payload_hash != record["content_hash"]:
                raise make_error(
                    ErrorCode.CACHE_CORRUPT,
                    "cached payload content hash does not match",
                    stage=str(record.get("node")),
                    stored_hash=record["content_hash"],
                    rebuilt_hash=rebuilt_payload_hash,
                    key=key,
                )
            rebuilt_key = self.build_key(record["node"], record["key_details"])
            if rebuilt_key != record["key"] or rebuilt_key != key:
                raise make_error(
                    ErrorCode.CACHE_CORRUPT,
                    "cached composite key does not match its components",
                    stage=str(record.get("node")),
                    version=record["key_details"].get("charset_version"),
                    key=key,
                )
        except (KeyError, json.JSONDecodeError) as exc:
            raise make_error(
                ErrorCode.CACHE_CORRUPT,
                "cache entry is unreadable or structurally damaged",
                key=key,
            ) from exc
        return CacheEntry(
            key=record["key"],
            node=record["node"],
            key_details=record["key_details"],
            payload=payload,
            content_hash=record["content_hash"],
        )

    def key_details(self, key: str) -> Dict[str, object]:
        """Export the composite key breakdown for diagnostic inspection."""

        entry = self.get(key)
        if entry is None:
            raise make_error(
                ErrorCode.CACHE_CORRUPT,
                "cannot export key details for missing entry",
                key=key,
            )
        return dict(entry.key_details)

    # ----------------------------------------------------------------- write
    def _acquire_stage(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._stage_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._stage_locks[key] = lock
            return lock

    def commit(
        self,
        node: str,
        key_details: Dict[str, object],
        payload: Dict[str, object],
        *,
        non_blocking: bool = True,
    ) -> CacheEntry:
        """Validate, content-hash and atomically commit an entry.

        With ``non_blocking=True`` (the default) a concurrent commit to the
        same key immediately raises ``E_STAGE_BUSY`` rather than waiting.
        Exactly one commit ever lands; the already committed entry is left
        intact.
        """

        key = self.build_key(node, key_details)
        lock = self._acquire_stage(key)
        acquired = lock.acquire(blocking=not non_blocking)
        if not acquired:
            raise make_error(
                ErrorCode.STAGE_BUSY,
                "another task is committing this cache key",
                stage=node,
                page_id=str(key_details.get("page_id")),
                key=key,
            )
        try:
            existing = self.get(key)
            if existing is not None:
                # Idempotent reuse: identical key always wins and is untouched.
                return existing
            self._enforce_entry_limit()
            payload_hash = content_hash(payload)
            record = {
                "key": key,
                "node": node,
                "key_details": key_details,
                "content_hash": payload_hash,
                "payload": payload,
            }
            self._atomic_write(self._path_for(key), record)
            return CacheEntry(
                key=key,
                node=node,
                key_details=dict(key_details),
                payload=payload,
                content_hash=payload_hash,
            )
        finally:
            lock.release()

    def _enforce_entry_limit(self) -> None:
        if self.entry_count() >= self.max_entries:
            raise make_error(
                ErrorCode.RESOURCE_LIMIT,
                "cache entry total exceeded the configured limit",
                stage="cache",
                limit=self.max_entries,
            )

    @staticmethod
    def _atomic_write(path: str, record: Dict[str, Any]) -> None:
        directory = os.path.dirname(path)
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                )
            os.replace(tmp_path, path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    # ------------------------------------------------------------------ stats
    def entry_count(self) -> int:
        total = 0
        for shard in os.listdir(self.entries_dir):
            shard_path = os.path.join(self.entries_dir, shard)
            if os.path.isdir(shard_path):
                total += len([n for n in os.listdir(shard_path) if n.endswith(".json")])
        return total

    def all_keys(self) -> List[str]:
        keys: List[str] = []
        for shard in os.listdir(self.entries_dir):
            shard_path = os.path.join(self.entries_dir, shard)
            if os.path.isdir(shard_path):
                keys.extend(
                    n[: -len(".json")]
                    for n in os.listdir(shard_path)
                    if n.endswith(".json")
                )
        return sorted(keys)

    def verify_entry_bytes(self, key: str) -> str:
        """Hash the raw on-disk bytes (used by corruption self-checks)."""

        path = self._path_for(key)
        with open(path, "rb") as handle:
            return hash_bytes(handle.read())

    def canonical_key_bytes(self, node: str, key_details: Dict[str, object]) -> bytes:
        return canonical_bytes({"node": node, "details": key_details})
