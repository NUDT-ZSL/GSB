"""Resident-memory ledger with FIFO shard eviction.

Memory accounting is intentionally explicit and platform independent: every
candidate and fusion detail row carries a fixed byte weight (see
:mod:`recall_fusion.budget`) so the ledger is reproducible on Linux, macOS and
Windows. When the resident set exceeds the configured limit, the oldest fully
merged query is flushed to a numbered shard file; only compact counters remain
in memory, which guarantees final statistics are never lost.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MemoryManager:
    limit_bytes: int
    shard_dir: str | Path
    ledger_path: str | Path
    resident: dict[str, dict] = field(default_factory=dict)
    resident_bytes: int = 0
    peak_resident_bytes: int = 0
    eviction_count: int = 0
    evicted_query_ids: list[str] = field(default_factory=list)
    _order: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        Path(self.shard_dir).mkdir(parents=True, exist_ok=True)
        Path(self.ledger_path).parent.mkdir(parents=True, exist_ok=True)
        self._ledger_handle = open(self.ledger_path, "w", encoding="utf-8", newline="\n")
        self._record(
            "init",
            {
                "memory_limit_bytes": self.limit_bytes,
                "shard_dir": str(self.shard_dir),
            },
        )

    def _record(self, event: str, payload: dict) -> None:
        entry = {"event": event, **payload}
        self._ledger_handle.write(
            json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n"
        )
        self._ledger_handle.flush()

    def retain(self, query_id: str, payload: dict, size_bytes: int) -> dict:
        """Add a merged query and evict oldest shards while over the limit."""
        self.resident[query_id] = payload
        self._order.append(query_id)
        self.resident_bytes += size_bytes
        self.peak_resident_bytes = max(self.peak_resident_bytes, self.resident_bytes)
        self._record(
            "retain",
            {"query_id": query_id, "bytes": size_bytes, "resident": self.resident_bytes},
        )
        while self.resident_bytes > self.limit_bytes and self._order:
            victim = self._order.pop(0)
            self._evict(victim)
        return {
            "resident_bytes": self.resident_bytes,
            "peak_resident_bytes": self.peak_resident_bytes,
            "eviction_count": self.eviction_count,
        }

    def _evict(self, query_id: str) -> None:
        payload = self.resident.pop(query_id)
        size_bytes = payload["plan"]["estimated_bytes"]
        shard_name = f"shard_{self.eviction_count:05d}.jsonl"
        shard_path = Path(self.shard_dir) / shard_name
        with open(shard_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
        self.resident_bytes -= size_bytes
        self.eviction_count += 1
        self.evicted_query_ids.append(query_id)
        self._record(
            "evict",
            {
                "query_id": query_id,
                "bytes": size_bytes,
                "shard": shard_name,
                "resident_after": self.resident_bytes,
                "reason": "resident_bytes_exceeds_limit_fifo",
            },
        )

    def drain(self) -> None:
        """Flush everything still resident so final output lives in shards."""
        while self._order:
            self._evict(self._order.pop(0))
        self._record("drain", {"eviction_count": self.eviction_count})

    def close(self) -> None:
        self._record(
            "close",
            {
                "peak_resident_bytes": self.peak_resident_bytes,
                "eviction_count": self.eviction_count,
            },
        )
        self._ledger_handle.close()

    def stats(self) -> dict:
        return {
            "memory_limit_bytes": self.limit_bytes,
            "peak_resident_bytes": self.peak_resident_bytes,
            "resident_bytes_final": self.resident_bytes,
            "eviction_count": self.eviction_count,
            "evicted_query_ids": list(self.evicted_query_ids),
        }
