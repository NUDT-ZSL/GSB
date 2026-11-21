"""Auditable invalidation events and cache hit/recompute coverage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class InvalidationEvent:
    event_id: int
    kind: str
    affected_sample_ids: tuple[str, ...]
    invalidated_cache_keys: tuple[str, ...]
    invalidated_sample_ids: tuple[str, ...]
    details: dict[str, Any] = field(default_factory=dict)
    cache_hit_sample_ids: set[str] = field(default_factory=set)
    recomputed_sample_ids: set[str] = field(default_factory=set)

    def _affected(self) -> set[str]:
        return set(self.affected_sample_ids)

    def mark_recomputed(self, sample_ids: Iterable[str]) -> None:
        affected = self._affected()
        for sample_id in sample_ids:
            if sample_id in affected and sample_id not in self.cache_hit_sample_ids:
                self.recomputed_sample_ids.add(sample_id)

    def mark_hit(self, sample_ids: Iterable[str]) -> None:
        affected = self._affected()
        for sample_id in sample_ids:
            if sample_id in affected and sample_id not in self.recomputed_sample_ids:
                self.cache_hit_sample_ids.add(sample_id)

    @property
    def pending_sample_ids(self) -> tuple[str, ...]:
        covered = self.cache_hit_sample_ids | self.recomputed_sample_ids
        return tuple(sorted(self._affected() - covered))

    @property
    def coverage_ok(self) -> bool:
        hit = self.cache_hit_sample_ids
        recomputed = self.recomputed_sample_ids
        return not (hit & recomputed) and (hit | recomputed) == self._affected()

    def to_dict(self) -> dict[str, Any]:
        pending = self.pending_sample_ids
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "affected_sample_ids": list(self.affected_sample_ids),
            "invalidated_cache_keys": list(self.invalidated_cache_keys),
            "invalidated_sample_ids": list(self.invalidated_sample_ids),
            "cache_hit_sample_ids": sorted(self.cache_hit_sample_ids),
            "recomputed_sample_ids": sorted(self.recomputed_sample_ids),
            "pending_sample_ids": list(pending),
            "coverage_ok": self.coverage_ok,
            "details": self.details,
        }

