"""Configurable resource ceilings and degradation bookkeeping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .errors import E_RESOURCE_LIMIT, DiagError


@dataclass
class ResourceLimits:
    """Ceilings on simultaneously resident results, cache entries and shards."""

    max_resident_results: int = 128
    max_cache_entries: int = 4096
    max_shards_per_result: int = 32

    def enforce_results(self, resident: int) -> None:
        if resident > self.max_resident_results:
            raise DiagError(
                E_RESOURCE_LIMIT,
                "resident result count exceeds configured ceiling",
                context={"resident": resident,
                         "limit": self.max_resident_results,
                         "category": "resident_results"},
            )

    def enforce_shards(self, result_ref: str, shard_count: int) -> None:
        if shard_count > self.max_shards_per_result:
            raise DiagError(
                E_RESOURCE_LIMIT,
                "shard count for a single result exceeds configured ceiling",
                context={"result_ref": result_ref,
                         "shards": shard_count,
                         "limit": self.max_shards_per_result,
                         "category": "shards_per_result"},
            )

    def enforce_cache(self, entries: int) -> None:
        if entries > self.max_cache_entries:
            raise DiagError(
                E_RESOURCE_LIMIT,
                "cache entry count exceeds configured ceiling",
                context={"entries": entries,
                         "limit": self.max_cache_entries,
                         "category": "cache_entries"},
            )


@dataclass
class DegradationLog:
    """Records resource-driven degradations instead of failing hard.

    The workbench may choose to degrade gracefully (for example by trimming the
    resident result set to the ceiling) rather than raising
    :data:`~diagwb.errors.E_RESOURCE_LIMIT`; each such event is logged here with
    a degradation category so it is visible in the diagnostic view.
    """

    events: List[dict] = field(default_factory=list)

    def record(self, category: str, detail: dict) -> None:
        self.events.append({"category": category, "detail": detail})

    def categories(self) -> List[str]:
        return sorted({event["category"] for event in self.events})
