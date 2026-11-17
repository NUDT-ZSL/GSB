"""Version-aware deterministic LRU cache for routing plans."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .errors import E_CACHE, E_SNAPSHOT, RouterError
from .util import canonical_json, sha256_text


@dataclass(frozen=True)
class RouteCacheKey:
    table: str
    normalized_predicate: str
    rule_version: int
    ring_version: int | None

    def canonical(self) -> str:
        return canonical_json(
            {
                "normalized_predicate": self.normalized_predicate,
                "ring_version": self.ring_version,
                "rule_version": self.rule_version,
                "table": self.table,
            }
        )

    def digest(self) -> str:
        return sha256_text(self.canonical())


@dataclass(frozen=True)
class CacheEntry:
    key: RouteCacheKey
    plan: dict[str, Any]
    recorded_rule_version: int
    recorded_ring_version: int | None
    inserted_seq: int
    accessed_seq: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "accessed_seq": self.accessed_seq,
            "inserted_seq": self.inserted_seq,
            "key": {
                "normalized_predicate": self.key.normalized_predicate,
                "ring_version": self.key.ring_version,
                "rule_version": self.key.rule_version,
                "table": self.key.table,
            },
            "plan": self.plan,
            "recorded_ring_version": self.recorded_ring_version,
            "recorded_rule_version": self.recorded_rule_version,
        }


@dataclass(frozen=True)
class CacheLookup:
    status: str
    code: str | None
    entry: CacheEntry | None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status}
        if self.code is not None:
            result["code"] = self.code
        if self.entry is not None:
            result["cache_key_digest"] = self.entry.key.digest()
        return result


class RoutingCache:
    """A bounded LRU cache whose eviction order is fully reproducible."""

    def __init__(self, capacity: int = 32) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise RouterError(code=E_CACHE, message="cache capacity must be a positive integer")
        self.capacity = capacity
        self._entries: dict[RouteCacheKey, CacheEntry] = {}
        self._clock = 0

    def __len__(self) -> int:
        return len(self._entries)

    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    def lookup(
        self,
        key: RouteCacheKey,
        *,
        current_rule_version: int,
        current_ring_version: int | None,
    ) -> CacheLookup:
        entry = self._entries.get(key)
        if entry is None:
            return CacheLookup(status="miss", code=None, entry=None)
        if (
            entry.recorded_rule_version != current_rule_version
            or entry.recorded_ring_version != current_ring_version
            or entry.key.rule_version != current_rule_version
            or entry.key.ring_version != current_ring_version
        ):
            removed = self._entries.pop(key, None)
            return CacheLookup(status="stale", code=E_CACHE, entry=removed)
        refreshed = replace(entry, accessed_seq=self._tick())
        self._entries[key] = refreshed
        return CacheLookup(status="hit", code=None, entry=refreshed)

    def put(
        self,
        key: RouteCacheKey,
        plan: dict[str, Any],
        *,
        recorded_rule_version: int | None = None,
        recorded_ring_version: int | None = None,
    ) -> dict[str, Any]:
        rule_version = key.rule_version if recorded_rule_version is None else recorded_rule_version
        ring_version = key.ring_version if recorded_ring_version is None else recorded_ring_version
        tick = self._tick()
        previous = self._entries.get(key)
        self._entries[key] = CacheEntry(
            key=key,
            plan=plan,
            recorded_rule_version=rule_version,
            recorded_ring_version=ring_version,
            inserted_seq=previous.inserted_seq if previous is not None else tick,
            accessed_seq=tick,
        )
        evicted: list[str] = []
        while len(self._entries) > self.capacity:
            victim = min(
                self._entries.values(),
                key=lambda entry: (entry.accessed_seq, entry.inserted_seq, entry.key.canonical()),
            )
            del self._entries[victim.key]
            evicted.append(victim.key.digest())
        return {"evicted": evicted, "size": len(self._entries)}

    def clear(self) -> int:
        count = len(self._entries)
        self._entries.clear()
        return count

    def remove_table(self, table_name: str) -> int:
        keys = sorted(
            (key for key in self._entries if key.table == table_name),
            key=lambda key: key.canonical(),
        )
        for key in keys:
            del self._entries[key]
        return len(keys)

    def remove_key(self, key: RouteCacheKey) -> bool:
        return self._entries.pop(key, None) is not None

    def entries(self) -> list[CacheEntry]:
        return sorted(self._entries.values(), key=lambda entry: (entry.key.canonical(), entry.inserted_seq))

    def to_dict(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "clock": self._clock,
            "entries": [entry.to_dict() for entry in self.entries()],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RoutingCache":
        if not isinstance(payload, Mapping):
            raise RouterError(code=E_SNAPSHOT, message="cache snapshot must be an object")
        capacity = payload.get("capacity", 32)
        try:
            cache = cls(capacity=capacity)  # type: ignore[arg-type]
        except RouterError as error:
            raise RouterError(code=E_SNAPSHOT, message=f"cache snapshot {error.message}") from error
        raw_entries = payload.get("entries", [])
        if not isinstance(raw_entries, list):
            raise RouterError(code=E_SNAPSHOT, message="cache snapshot entries must be an array")
        clock = payload.get("clock", 0)
        if isinstance(clock, bool) or not isinstance(clock, int) or clock < 0:
            raise RouterError(code=E_SNAPSHOT, message="cache snapshot clock must be a non-negative integer")
        for index, raw in enumerate(raw_entries):
            if not isinstance(raw, Mapping):
                raise RouterError(code=E_SNAPSHOT, message=f"cache entry {index} must be an object")
            raw_key = raw.get("key")
            if not isinstance(raw_key, Mapping):
                raise RouterError(code=E_SNAPSHOT, message=f"cache entry {index} key must be an object")
            try:
                key = RouteCacheKey(
                    table=str(raw_key["table"]),
                    normalized_predicate=str(raw_key["normalized_predicate"]),
                    rule_version=int(raw_key["rule_version"]),
                    ring_version=None if raw_key.get("ring_version") is None else int(raw_key["ring_version"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise RouterError(code=E_SNAPSHOT, message=f"cache entry {index} has an invalid key") from error
            plan = raw.get("plan")
            if not isinstance(plan, Mapping):
                raise RouterError(code=E_SNAPSHOT, message=f"cache entry {index} plan must be an object")
            cache._entries[key] = CacheEntry(
                key=key,
                plan=dict(plan),
                recorded_rule_version=int(raw.get("recorded_rule_version", key.rule_version)),
                recorded_ring_version=(
                    None
                    if raw.get("recorded_ring_version", key.ring_version) is None
                    else int(raw.get("recorded_ring_version", key.ring_version))
                ),
                inserted_seq=int(raw.get("inserted_seq", 0)),
                accessed_seq=int(raw.get("accessed_seq", 0)),
            )
        while len(cache._entries) > cache.capacity:
            victim = min(
                cache._entries.values(),
                key=lambda entry: (entry.accessed_seq, entry.inserted_seq, entry.key.canonical()),
            )
            del cache._entries[victim.key]
        cache._clock = max([clock] + [entry.accessed_seq for entry in cache._entries.values()])
        return cache
