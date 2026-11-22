from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from .errors import E_CACHE_CORRUPT, WorkbenchError
from .util import canonical_json, content_hash


@dataclass
class CacheRecord:
    key: str
    payload: dict[str, Any]
    checksum: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ContentAddressedCache:
    """LRU plus optional disk-backed content-addressed cache."""

    def __init__(self, max_entries: int, root: str | Path | None = None) -> None:
        self.max_entries = max(1, int(max_entries))
        self.root = Path(root) if root else None
        if self.root:
            self.root.mkdir(parents=True, exist_ok=True)
        self._records: OrderedDict[str, CacheRecord] = OrderedDict()
        self._lock = RLock()
        self.stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "invalidations": 0,
            "corruptions": 0,
            "writes": 0,
            "lru_size": 0,
        }

    def _disk_path(self, key: str) -> Path | None:
        if not self.root:
            return None
        return self.root / f"{key}.json"

    def _verify(self, key: str, payload: Any, checksum: str) -> None:
        actual = content_hash(payload)
        if actual != checksum:
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "缓存内容校验和不符",
                stage="cache_read",
                details={"key": key, "expected": checksum, "actual": actual},
            )

    def get(self, key: str, verify: bool = True) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(key)
            if record is None:
                path = self._disk_path(key)
                if path and path.exists():
                    try:
                        envelope = json.loads(path.read_text(encoding="utf-8"))
                        record = CacheRecord(
                            key=envelope["key"],
                            payload=envelope["payload"],
                            checksum=envelope["checksum"],
                            metadata=envelope.get("metadata", {}),
                        )
                        if record.key != key:
                            raise ValueError("cache key mismatch")
                    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
                        self.stats["corruptions"] += 1
                        raise WorkbenchError(
                            E_CACHE_CORRUPT,
                            "缓存文件损坏",
                            stage="cache_read",
                            details={"key": key, "reason": str(exc)},
                        ) from exc
                    self._records[key] = record
            if record is None:
                self.stats["misses"] += 1
                return None
            if verify:
                try:
                    self._verify(key, record.payload, record.checksum)
                except WorkbenchError:
                    self.stats["corruptions"] += 1
                    self._records.pop(key, None)
                    path = self._disk_path(key)
                    if path and path.exists():
                        path.unlink(missing_ok=True)
                    raise
            self._records.move_to_end(key)
            self.stats["hits"] += 1
            return record.payload

    def put(
        self,
        key: str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        checksum = content_hash(payload)
        record = CacheRecord(
            key=key,
            payload=payload,
            checksum=checksum,
            metadata=metadata or {},
        )
        with self._lock:
            self._records[key] = record
            self._records.move_to_end(key)
            self.stats["writes"] += 1
            while len(self._records) > self.max_entries:
                evicted_key, _ = self._records.popitem(last=False)
                self.stats["evictions"] += 1
                path = self._disk_path(evicted_key)
                if path and path.exists():
                    path.unlink(missing_ok=True)
            path = self._disk_path(key)
            if path:
                envelope = {
                    "key": key,
                    "checksum": checksum,
                    "metadata": metadata or {},
                    "payload": payload,
                }
                path.write_text(
                    canonical_json(envelope),
                    encoding="utf-8",
                )
            self.stats["lru_size"] = len(self._records)

    def invalidate(self, keys: list[str]) -> list[str]:
        invalidated: list[str] = []
        with self._lock:
            for key in keys:
                if self._records.pop(key, None) is not None:
                    self.stats["invalidations"] += 1
                path = self._disk_path(key)
                if path and path.exists():
                    path.unlink(missing_ok=True)
                invalidated.append(key)
            self.stats["lru_size"] = len(self._records)
        return invalidated

    def clear(self) -> None:
        with self._lock:
            keys = list(self._records)
            self.invalidate(keys)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._records)

    def snapshot_stats(self) -> dict[str, Any]:
        with self._lock:
            return {**self.stats, "lru_size": len(self._records)}

