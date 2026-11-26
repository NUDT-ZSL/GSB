"""Content-addressed stage cache with deterministic LRU eviction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import (
    atomic_write_bytes,
    canonical_bytes,
    content_hash,
    read_json,
)
from .config import DegradationLog
from .errors import corrupt


class ContentCache:
    def __init__(
        self,
        root: str | Path,
        *,
        max_entries: int,
        degradation: DegradationLog | None = None,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.max_entries = max_entries
        self.degradation = degradation or DegradationLog()
        self.stats = {"hits": 0, "misses": 0, "puts": 0, "evictions": 0, "invalid": 0}
        self._sequence = 0
        self._entries: dict[str, dict[str, Any]] = {}
        self._load_index()

    def _load_index(self) -> None:
        if not self.index_path.exists():
            self._entries = {}
            self._save_index()
            return
        try:
            data = read_json(self.index_path)
            entries = data.get("entries", {})
            sequence = int(data.get("sequence", 0))
            persisted_stats = data.get("stats", {})
            if not isinstance(entries, dict):
                raise ValueError("cache index entries must be an object")
        except Exception as exc:
            raise corrupt(
                f"cache index is invalid: {exc}",
                field="cache_index",
                details={"path": str(self.index_path)},
            )
        self._entries = entries
        self._sequence = sequence
        if isinstance(persisted_stats, dict):
            for key in self.stats:
                value = persisted_stats.get(key)
                if isinstance(value, int) and value >= 0:
                    self.stats[key] = value

    def _save_index(self) -> None:
        data = {
            "schema_version": "cache-v1",
            "sequence": self._sequence,
            "stats": dict(self.stats),
            "entries": {
                key: {
                    "path": entry["path"],
                    "checksum": entry["checksum"],
                    "size": entry["size"],
                    "last_used": entry["last_used"],
                    "task_id": entry["task_id"],
                    "stage": entry["stage"],
                }
                for key, entry in sorted(self._entries.items())
            },
        }
        atomic_write_bytes(self.index_path, canonical_bytes(data))

    def _artifact_path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None:
            self.stats["misses"] += 1
            return None
        path = self.root / str(entry["path"])
        if not path.is_file():
            self._remove_entry(key, delete_file=False)
            self.stats["invalid"] += 1
            self.stats["misses"] += 1
            return None
        try:
            envelope = read_json(path)
            if envelope.get("cache_key") != key:
                raise ValueError("cache key mismatch")
            payload = envelope.get("payload")
            checksum = content_hash(payload)
            if checksum != envelope.get("checksum") or checksum != entry["checksum"]:
                raise ValueError("cache checksum mismatch")
        except Exception:
            self._remove_entry(key, delete_file=True)
            self.stats["invalid"] += 1
            self.stats["misses"] += 1
            return None
        self._sequence += 1
        entry["last_used"] = self._sequence
        self._save_index()
        self.stats["hits"] += 1
        return payload

    def put(
        self,
        key: str,
        payload: dict[str, Any],
        *,
        task_id: str,
        stage: str,
    ) -> None:
        body = {"cache_key": key, "payload": payload}
        body["checksum"] = content_hash(payload)
        raw = canonical_bytes(body)
        path = self._artifact_path(key)
        atomic_write_bytes(path, raw)
        self._sequence += 1
        self._entries[key] = {
            "path": path.name,
            "checksum": body["checksum"],
            "size": len(raw),
            "last_used": self._sequence,
            "task_id": task_id,
            "stage": stage,
        }
        self.stats["puts"] += 1
        while len(self._entries) > self.max_entries:
            victim = min(
                self._entries,
                key=lambda candidate: (
                    self._entries[candidate]["last_used"],
                    candidate,
                ),
            )
            if victim == key and len(self._entries) == 1:
                break
            removed = self._entries[victim]
            self._remove_entry(victim, delete_file=True)
            self.stats["evictions"] += 1
            self.degradation.record(
                reason="cache_entry_limit",
                priority=2,
                action="evict_least_recently_used",
                task_id=task_id,
                details={
                    "evicted_key": victim,
                    "evicted_task": removed.get("task_id"),
                    "limit": self.max_entries,
                },
            )
        self._save_index()

    def _remove_entry(self, key: str, *, delete_file: bool) -> None:
        entry = self._entries.pop(key, None)
        if delete_file and entry is not None:
            path = self.root / str(entry["path"])
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def summary(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "sequence": self._sequence,
            **self.stats,
        }

    def entries_by_task(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for key, entry in sorted(self._entries.items()):
            result.setdefault(str(entry["task_id"]), []).append(
                {
                    "key": key,
                    "stage": entry["stage"],
                    "last_used": entry["last_used"],
                    "size": entry["size"],
                }
            )
        return result


__all__ = ["ContentCache"]

