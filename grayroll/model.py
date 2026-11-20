from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CandidateBatch:
    service: str
    batch_id: str
    source_version: str
    source_priority: int
    logical_clock_ms: int
    generation: int
    tiers: tuple[float, ...]
    observation_markers: tuple[float, ...]
    earliest_rollout_ms: int
    criteria: dict[str, Any]
    rollback_start_ms: int
    rollback_end_ms: int
    rollback_step_ms: int
    depends_on: tuple[str, ...]
    raw: dict[str, Any]
    migration: dict[str, Any]


@dataclass(frozen=True)
class LoadedPackage:
    manifest: dict[str, Any]
    manifest_hash: str
    defaults: dict[str, Any]
    candidates: tuple[CandidateBatch, ...]
    package_id: str

    @property
    def version(self) -> int:
        return int(self.manifest["package_version"])

    @property
    def generation_floor(self) -> int:
        return int(self.manifest["generation_floor"])

    @property
    def service(self) -> str:
        return str(self.manifest["service"])

    @property
    def tier_digest(self) -> str:
        return str(self.manifest["tier_digest"])


@dataclass
class MergeConflict:
    service: str
    batch_id: str
    evidence: dict[str, Any]


@dataclass
class UnifiedBatch:
    service: str
    batch_id: str
    source_version: str
    source_priority: int
    logical_clock_ms: int
    generation: int
    tiers: tuple[float, ...]
    observation_markers: tuple[float, ...]
    earliest_rollout_ms: int
    criteria: dict[str, Any]
    rollback_start_ms: int
    rollback_end_ms: int
    rollback_step_ms: int
    depends_on: tuple[str, ...]
    raw: dict[str, Any]
    decision: dict[str, Any]
    migration: dict[str, Any]
    sort_key: tuple[tuple[int, Any], ...] = field(init=False)

    def __post_init__(self) -> None:
        from .util import natural_key

        self.sort_key = natural_key(self.batch_id)


@dataclass
class MergeResult:
    batches: list[UnifiedBatch]
    conflicts: list[MergeConflict]

    def summary(self) -> dict[str, Any]:
        return {
            "batch_count": len(self.batches),
            "conflict_count": len(self.conflicts),
            "batches": [
                {
                    "batch_id": batch.batch_id,
                    "decision": batch.decision["decision"],
                    "earliest_rollout_ms": batch.earliest_rollout_ms,
                    "service": batch.service,
                    "source_version": batch.source_version,
                    "tiers": list(batch.tiers),
                }
                for batch in self.batches
            ],
            "migrations": [
                {
                    "batch_id": batch.batch_id,
                    "migration": batch.migration,
                    "service": batch.service,
                }
                for batch in self.batches
                if batch.source_version == "v1"
            ],
        }
