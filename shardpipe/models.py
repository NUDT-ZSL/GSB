from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    sample_count: int
    feature_dim: int
    sample_bytes: int
    cacheable: bool
    shard_size: int
    version: str
    generation_params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def shard_count(self) -> int:
        return self.sample_count // self.shard_size

    @property
    def shard_bytes(self) -> int:
        return self.sample_bytes * self.shard_size


@dataclass(frozen=True)
class CacheEntrySpec:
    dataset: str
    shard_index: int
    generation: int
    bytes: int
    last_used_clock: int
    digest: str | None = None
    generation_fingerprint: str | None = None


@dataclass(frozen=True)
class CacheViewSpec:
    generation: int
    entries: tuple[CacheEntrySpec, ...]
    version: str = "cache-v1"


@dataclass(frozen=True)
class JobSpec:
    name: str
    dataset: str
    batch_size: int
    epochs: int
    data_parallelism: int
    prefetch_depth: int
    allow_shard_reuse: bool


@dataclass(frozen=True)
class ScheduleConfig:
    logical_time_budget_ms: int
    per_dataset_prefetch_limit: int
    global_memory_budget_mib: int
    cache_capacity_entries: int
    tolerance_ms: int
    logical_clock_step_ms: int
    logical_clock_start_ms: int = 0
    generation_base_ms: int = 1
    generation_bytes_per_ms: int = 4096
    cache_hit_ms: int = 0

    @property
    def global_memory_budget_bytes(self) -> int:
        return self.global_memory_budget_mib * 1024 * 1024


@dataclass(frozen=True)
class Workload:
    global_seed: int
    datasets: tuple[DatasetSpec, ...]
    cache: CacheViewSpec
    jobs: tuple[JobSpec, ...]
    config: ScheduleConfig
    canonical: Mapping[str, Any]
    digest: str

    def dataset_map(self) -> dict[str, DatasetSpec]:
        return {dataset.name: dataset for dataset in self.datasets}

    def job_map(self) -> dict[str, JobSpec]:
        return {job.name: job for job in self.jobs}


@dataclass(frozen=True)
class SampleRef:
    dataset: str
    sample_index: int
    shard_index: int
    offset_in_shard: int


@dataclass(frozen=True)
class BatchPlan:
    batch_id: str
    batch_key: str
    job_name: str
    dataset: str
    epoch: int
    worker_id: int
    local_index: int
    global_ordinal: int
    wave_index: int
    samples: tuple[SampleRef, ...]
    source_shards: tuple[int, ...]


@dataclass(frozen=True)
class WorkerPlan:
    worker_id: int
    shard_indices: tuple[int, ...]
    batches: tuple[BatchPlan, ...]


@dataclass(frozen=True)
class JobPlan:
    job: JobSpec
    dataset: DatasetSpec
    dataset_fingerprint: str
    workers: tuple[WorkerPlan, ...]
    batches: tuple[BatchPlan, ...]
    shard_to_workers: Mapping[int, tuple[int, ...]]


@dataclass(frozen=True)
class ShardDemand:
    dataset: str
    shard_index: int
    cache_generation: int
    generation_fingerprint: str
    bytes: int
    wave_index: int
    earliest_global_ordinal: int
    worker_ids: tuple[int, ...]
    batch_ids: tuple[str, ...]


@dataclass(frozen=True)
class CacheKey:
    dataset: str
    shard_index: int
    cache_generation: int
    generation_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "shard_index": self.shard_index,
            "cache_generation": self.cache_generation,
            "generation_fingerprint": self.generation_fingerprint,
        }

    def identity(self) -> str:
        return (
            f"{self.dataset}:{self.shard_index}:"
            f"{self.cache_generation}:{self.generation_fingerprint}"
        )
