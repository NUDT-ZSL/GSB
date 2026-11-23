"""Resident-memory accounting and shard-LRU eviction."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import sys

from .models import QueryResult


def _sizeof(obj) -> int:
    """Deterministic, conservative byte estimate (no id-based caching)."""
    seen: set[int] = set()

    def walk(value) -> int:
        marker = id(value)
        if marker in seen:
            return 0
        seen.add(marker)
        total = sys.getsizeof(value)
        if isinstance(value, dict):
            for key, item in value.items():
                total += walk(key) + walk(item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                total += walk(item)
        elif is_dataclass(value) and not isinstance(value, type):
            for dc_field in fields(value):
                total += walk(getattr(value, dc_field.name))
        elif hasattr(value, "__dict__"):
            for item in vars(value).values():
                total += walk(item)
        return total

    return walk(obj)


def result_resident_bytes(result: QueryResult) -> int:
    """Bytes of the bulky per-query shard (candidate arrays + merge tables)."""
    return _sizeof(result.channel_candidates) + _sizeof(result.merged)


def result_total_bytes(result: QueryResult) -> int:
    return _sizeof(result)


@dataclass
class EvictionEvent:
    trigger_query_id: str
    evicted_query_ids: list[str]
    released_bytes: int
    retained_bytes: int
    limit_bytes: int
    compacted_results: dict[str, "QueryResult"] | None = None


class ResidentMemoryManager:
    """Tracks live query shards; evicts oldest completed shards (shard LRU)."""

    def __init__(self, limit_bytes: int, policy: str = "shard_lru"):
        if policy != "shard_lru":
            raise ValueError(f"unsupported eviction policy {policy!r}")
        self.limit_bytes = limit_bytes
        self.policy = policy
        self._live: dict[str, QueryResult] = {}
        self._order: list[str] = []
        self.resident_bytes = 0
        self.peak_bytes = 0
        self.events: list[EvictionEvent] = []
        self.eviction_count = 0

    def register(self, result: QueryResult) -> list[EvictionEvent]:
        events: list[EvictionEvent] = []
        incoming = result_resident_bytes(result)
        while (
            self.resident_bytes + incoming > self.limit_bytes and self._order
        ):
            oldest_id = self._order.pop(0)
            oldest = self._live.pop(oldest_id)
            released = result_resident_bytes(oldest)
            self.resident_bytes -= released
            compact = oldest.compact_for_eviction()
            # The compact result stays outside the live shard map; callers keep
            # the compact copy in their aggregate store, retaining counts/traces.
            self.eviction_count += 1
            event = EvictionEvent(
                trigger_query_id=result.query_id,
                evicted_query_ids=[oldest_id],
                released_bytes=released,
                retained_bytes=self.resident_bytes + incoming,
                limit_bytes=self.limit_bytes,
                compacted_results={oldest_id: compact},
            )
            self.events.append(event)
            events.append(event)
        if self.resident_bytes + incoming > self.limit_bytes:
            # A single shard larger than the whole limit cannot be admitted.
            from .errors import ErrorCode, PipelineError

            raise PipelineError(
                ErrorCode.E_RESOURCE_LIMIT,
                stage="resident_memory",
                message=(
                    f"shard for {result.query_id} needs {incoming} bytes, "
                    f"limit is {self.limit_bytes} bytes and no shard remains to evict"
                ),
                query_id=result.query_id,
                context={
                    "shard_bytes": incoming,
                    "limit_bytes": self.limit_bytes,
                },
            )
        self._live[result.query_id] = result
        self._order.append(result.query_id)
        self.resident_bytes += incoming
        self.peak_bytes = max(self.peak_bytes, self.resident_bytes)
        return events

    def compact_all(self) -> dict[str, QueryResult]:
        compacted = {
            query_id: result.compact_for_eviction()
            for query_id, result in self._live.items()
        }
        self._live.clear()
        self._order.clear()
        self.resident_bytes = 0
        return compacted

    def live_query_ids(self) -> list[str]:
        return list(self._order)
