"""Configurable resource limits and resident-memory estimation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from .errors import DriftError, ErrorCode


@dataclass
class ResourceLimits:
    max_batch_samples: int = 100_000
    max_cache_entries: int = 512
    max_resident_bytes: int = 64 * 1024 * 1024
    degrade_on_resident_overflow: bool = True


@dataclass
class ResourceGuard:
    limits: ResourceLimits
    degradation_marks: List[Dict[str, Any]] = field(default_factory=list)

    def check_batch_size(self, sample_count: int, batch_id: int) -> None:
        if sample_count > self.limits.max_batch_samples:
            raise DriftError(
                ErrorCode.RESOURCE_LIMIT,
                "batch sample count exceeds the configured limit",
                {
                    "field": "max_batch_samples",
                    "batch_id": batch_id,
                    "limit": self.limits.max_batch_samples,
                    "actual": sample_count,
                },
            )

    def check_cache_entries(self, current: int) -> None:
        if current > self.limits.max_cache_entries:
            raise DriftError(
                ErrorCode.RESOURCE_LIMIT,
                "cache entry count exceeds the configured limit",
                {
                    "field": "max_cache_entries",
                    "limit": self.limits.max_cache_entries,
                    "actual": current,
                },
            )

    def estimate_resident_bytes(self, batch_stats: List[Any]) -> int:
        """Deterministic upper-bound estimate of resident statistic memory.

        Numeric sorted values dominate the footprint (8 bytes each), category
        frequency maps are approximated per entry, and per-record bookkeeping
        uses a fixed overhead so the estimate never depends on the allocator.
        """
        total = 0
        record_overhead = 256
        for record in batch_stats:
            total += record_overhead
            for stat in record.numeric.values():
                total += 64 + stat.count * 8
            for stat in record.categorical.values():
                total += 64 + len(stat.frequencies) * 72
        return total

    def check_resident(self, batch_stats: List[Any]) -> Tuple[bool, int]:
        estimate = self.estimate_resident_bytes(batch_stats)
        if estimate <= self.limits.max_resident_bytes:
            return False, estimate
        if self.limits.degrade_on_resident_overflow:
            self.degradation_marks.append(
                {
                    "category": "resident_statistics",
                    "field": "max_resident_bytes",
                    "limit": self.limits.max_resident_bytes,
                    "estimated": estimate,
                    "action": "degraded_window_only_retention",
                }
            )
            return True, estimate
        raise DriftError(
            ErrorCode.RESOURCE_LIMIT,
            "resident statistic memory estimate exceeds the limit",
            {
                "field": "max_resident_bytes",
                "limit": self.limits.max_resident_bytes,
                "estimated": estimate,
            },
        )
