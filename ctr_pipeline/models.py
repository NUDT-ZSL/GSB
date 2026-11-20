from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FeatureSpec:
    feature_index: int
    dimension: int
    feature_type: str
    cardinality: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_index": self.feature_index,
            "dimension": self.dimension,
            "type": self.feature_type,
            "cardinality": self.cardinality,
        }


@dataclass(frozen=True)
class FeatureDictionary:
    entries: dict[int, FeatureSpec]
    version: str
    checksum: str

    @property
    def ordered_indices(self) -> tuple[int, ...]:
        return tuple(sorted(self.entries))


@dataclass(frozen=True)
class CrossSlot:
    slot: int
    left: int
    right: int


@dataclass(frozen=True)
class SourceSample:
    sample_id: str
    user_indices: tuple[int, ...]
    user_values: tuple[tuple[float, ...], ...]
    item_indices: tuple[int, ...]
    item_values: tuple[tuple[float, ...], ...]
    cross_slots: tuple[CrossSlot, ...]
    label: int
    line_number: int


@dataclass(frozen=True)
class ExpandedSample:
    sample_id: str
    label: int
    feature_dimension: int
    indices: tuple[int, ...]
    values: tuple[float, ...]

    @property
    def nnz(self) -> int:
        return len(self.indices)

    def to_row(self) -> dict[str, Any]:
        return {
            "indices": list(self.indices),
            "label": self.label,
            "sample_id": self.sample_id,
            "values": list(self.values),
        }


@dataclass(frozen=True)
class ExpandedDataset:
    samples: tuple[ExpandedSample, ...]
    feature_dimension: int
    dictionary_version: str
    dictionary_checksum: str
    checksum: str

    @property
    def sample_ids(self) -> tuple[str, ...]:
        return tuple(sample.sample_id for sample in self.samples)

    @property
    def total_nnz(self) -> int:
        return sum(sample.nnz for sample in self.samples)


@dataclass(frozen=True)
class TrainingConfig:
    chunk_size: int = 8
    batch_sample_size: int = 24
    workers: int = 2
    max_rounds: int = 16
    learning_rate: float = 0.35
    tolerance: float = 1e-9
    l2: float = 0.0
    fault_chunk_ids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_sample_size": self.batch_sample_size,
            "chunk_size": self.chunk_size,
            "fault_chunk_ids": list(self.fault_chunk_ids),
            "l2": self.l2,
            "learning_rate": self.learning_rate,
            "max_rounds": self.max_rounds,
            "tolerance": self.tolerance,
            "workers": self.workers,
        }


@dataclass(frozen=True)
class BudgetConfig:
    total_mac_budget: int = 50_000_000
    memory_budget_bytes: int = 32 * 1024 * 1024
    worker_overhead_macs_per_round: int = 2_000
    worker_memory_bytes: int = 64 * 1024
    sample_base_bytes: int = 192
    nnz_bytes: int = 24
    score_bytes: int = 48
    chunk_overhead_bytes: int = 128
    allow_degraded: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_degraded": self.allow_degraded,
            "chunk_overhead_bytes": self.chunk_overhead_bytes,
            "memory_budget_bytes": self.memory_budget_bytes,
            "nnz_bytes": self.nnz_bytes,
            "sample_base_bytes": self.sample_base_bytes,
            "score_bytes": self.score_bytes,
            "total_mac_budget": self.total_mac_budget,
            "worker_memory_bytes": self.worker_memory_bytes,
            "worker_overhead_macs_per_round": self.worker_overhead_macs_per_round,
        }


@dataclass
class BudgetDecision:
    requested_chunk_size: int
    effective_chunk_size: int
    requested_workers: int
    effective_workers: int
    admitted_sample_ids: tuple[str, ...]
    rejected_sample_ids: tuple[str, ...]
    admitted_chunk_count: int
    total_chunk_count: int
    estimated_macs: int
    estimated_memory_bytes: int
    degraded: bool
    degrade_categories: tuple[str, ...]
    error: dict[str, Any] | None
    report: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrainingChunk:
    chunk_id: int
    batch_index: int
    chunk_index: int
    start: int
    end: int
    samples: tuple[ExpandedSample, ...]

    @property
    def sample_count(self) -> int:
        return len(self.samples)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_index": self.batch_index,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "end": self.end,
            "sample_count": self.sample_count,
            "sample_ids": [sample.sample_id for sample in self.samples],
            "start": self.start,
        }
