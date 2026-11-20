from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .determinism import (
    batch_cache_key,
    batch_digest_and_digests,
    dataset_fingerprint,
    sample_digest,
)
from .errors import BusinessError, ErrorCode
from .models import (
    BatchPlan,
    DatasetSpec,
    JobPlan,
    JobSpec,
    SampleRef,
    ShardDemand,
    WorkerPlan,
    Workload,
)


def _sample_ref(dataset: DatasetSpec, sample_index: int) -> SampleRef:
    return SampleRef(
        dataset=dataset.name,
        sample_index=sample_index,
        shard_index=sample_index // dataset.shard_size,
        offset_in_shard=sample_index % dataset.shard_size,
    )


def _sources(samples: Iterable[SampleRef]) -> tuple[int, ...]:
    seen: set[int] = set()
    result: list[int] = []
    for sample in samples:
        if sample.shard_index not in seen:
            seen.add(sample.shard_index)
            result.append(sample.shard_index)
    return tuple(result)


def _make_batch(
    *,
    job: JobSpec,
    dataset: DatasetSpec,
    fingerprint: str,
    epoch: int,
    worker_id: int,
    local_index: int,
    global_ordinal: int,
    sample_indices: tuple[int, ...],
    wave_window: int,
) -> BatchPlan:
    samples = tuple(_sample_ref(dataset, index) for index in sample_indices)
    return BatchPlan(
        batch_id=(
            f"{job.name}:e{epoch:05d}:w{worker_id:05d}:b{local_index:08d}"
        ),
        batch_key=batch_cache_key(
            job_name=job.name,
            dataset_name=dataset.name,
            dataset_fingerprint_value=fingerprint,
            batch_size=job.batch_size,
            epoch=epoch,
            worker_id=worker_id,
            local_index=local_index,
            sample_indices=sample_indices,
        ),
        job_name=job.name,
        dataset=dataset.name,
        epoch=epoch,
        worker_id=worker_id,
        local_index=local_index,
        global_ordinal=global_ordinal,
        wave_index=local_index // wave_window,
        samples=samples,
        source_shards=_sources(samples),
    )


def _validate_disjoint_workers(
    *,
    batches: tuple[BatchPlan, ...],
    data_parallelism: int,
) -> None:
    owner: dict[tuple[str, int], int] = {}
    for batch in batches:
        for sample in batch.samples:
            key = (sample.dataset, sample.sample_index)
            existing = owner.get(key)
            if existing is not None and existing != batch.worker_id:
                raise BusinessError(
                    ErrorCode.INVALID_WORKLOAD,
                    "batch plan assigns the same sample to multiple workers",
                    details={
                        "dataset": sample.dataset,
                        "sample_index": sample.sample_index,
                        "workers": sorted([existing, batch.worker_id]),
                    },
                )
            owner[key] = batch.worker_id
    invalid_workers = sorted({batch.worker_id for batch in batches if batch.worker_id >= data_parallelism})
    if invalid_workers:
        raise BusinessError(
            ErrorCode.INVALID_WORKLOAD,
            "batch plan contains worker ids outside data_parallelism",
            details={"worker_ids": invalid_workers},
        )


def build_job_plan(workload: Workload, job: JobSpec) -> JobPlan:
    dataset = workload.dataset_map()[job.dataset]
    fingerprint = dataset_fingerprint(workload.global_seed, dataset)
    wave_window = max(1, job.prefetch_depth)
    worker_batches: list[list[BatchPlan]] = [
        [] for _ in range(job.data_parallelism)
    ]
    all_batches: list[BatchPlan] = []

    if job.allow_shard_reuse:
        # Shared-shard mode: the global batch stream is round-robin assigned
        # to workers.  A shard straddling a batch boundary can therefore be
        # shared by multiple workers, while samples remain disjoint.
        next_local = [0 for _ in range(job.data_parallelism)]
        ordinal = 0
        for epoch in range(job.epochs):
            for start in range(0, dataset.sample_count, job.batch_size):
                end = min(dataset.sample_count, start + job.batch_size)
                worker = ordinal % job.data_parallelism
                local = next_local[worker]
                next_local[worker] += 1
                batch = _make_batch(
                    job=job,
                    dataset=dataset,
                    fingerprint=fingerprint,
                    epoch=epoch,
                    worker_id=worker,
                    local_index=local,
                    global_ordinal=ordinal,
                    sample_indices=tuple(range(start, end)),
                    wave_window=wave_window,
                )
                worker_batches[worker].append(batch)
                all_batches.append(batch)
                ordinal += 1
    else:
        # Exclusive-shard mode: shard ownership is round-robin and each shard
        # appears in exactly one worker stream.  This makes physical shard
        # reuse unnecessary.
        shards_by_worker: list[list[int]] = [
            [] for _ in range(job.data_parallelism)
        ]
        for shard_index in range(dataset.shard_count):
            shards_by_worker[shard_index % job.data_parallelism].append(shard_index)
        ordinal = 0
        for epoch in range(job.epochs):
            for worker_id, shard_indices in enumerate(shards_by_worker):
                indices: list[int] = []
                for shard_index in shard_indices:
                    start = shard_index * dataset.shard_size
                    indices.extend(range(start, start + dataset.shard_size))
                for local, start in enumerate(
                    range(0, len(indices), job.batch_size)
                ):
                    sample_indices = tuple(indices[start : start + job.batch_size])
                    if not sample_indices:
                        continue
                    batch = _make_batch(
                        job=job,
                        dataset=dataset,
                        fingerprint=fingerprint,
                        epoch=epoch,
                        worker_id=worker_id,
                        local_index=local,
                        global_ordinal=ordinal,
                        sample_indices=sample_indices,
                        wave_window=wave_window,
                    )
                    worker_batches[worker_id].append(batch)
                    all_batches.append(batch)
                    ordinal += 1

    batches = tuple(all_batches)
    _validate_disjoint_workers(batches=batches, data_parallelism=job.data_parallelism)

    workers: list[WorkerPlan] = []
    shard_to_workers: dict[int, set[int]] = {}
    for worker_id, worker_batch_list in enumerate(worker_batches):
        ordered_shards: list[int] = []
        seen: set[int] = set()
        for batch in worker_batch_list:
            for shard_index in batch.source_shards:
                if shard_index not in seen:
                    seen.add(shard_index)
                    ordered_shards.append(shard_index)
                shard_to_workers.setdefault(shard_index, set()).add(worker_id)
        workers.append(
            WorkerPlan(
                worker_id=worker_id,
                shard_indices=tuple(ordered_shards),
                batches=tuple(worker_batch_list),
            )
        )

    frozen_mapping = {
        shard_index: tuple(sorted(worker_ids))
        for shard_index, worker_ids in sorted(shard_to_workers.items())
    }
    return JobPlan(
        job=job,
        dataset=dataset,
        dataset_fingerprint=fingerprint,
        workers=tuple(workers),
        batches=batches,
        shard_to_workers=frozen_mapping,
    )


def build_shard_demands(plan: JobPlan, cache_generation: int) -> tuple[ShardDemand, ...]:
    by_shard: dict[int, dict[str, Any]] = {}
    for batch in plan.batches:
        for shard_index in batch.source_shards:
            state = by_shard.setdefault(
                shard_index,
                {
                    "wave": batch.wave_index,
                    "ordinal": batch.global_ordinal,
                    "workers": set(),
                    "batches": [],
                },
            )
            if batch.wave_index < state["wave"]:
                state["wave"] = batch.wave_index
            if batch.global_ordinal < state["ordinal"]:
                state["ordinal"] = batch.global_ordinal
            state["workers"].add(batch.worker_id)
            state["batches"].append(batch.batch_id)

    demands: list[ShardDemand] = []
    for shard_index in sorted(by_shard):
        state = by_shard[shard_index]
        demands.append(
            ShardDemand(
                dataset=plan.dataset.name,
                shard_index=shard_index,
                cache_generation=cache_generation,
                generation_fingerprint=plan.dataset_fingerprint,
                bytes=plan.dataset.shard_bytes,
                wave_index=state["wave"],
                earliest_global_ordinal=state["ordinal"],
                worker_ids=tuple(sorted(state["workers"])),
                batch_ids=tuple(sorted(state["batches"])),
            )
        )
    return tuple(
        sorted(
            demands,
            key=lambda demand: (
                demand.wave_index,
                demand.earliest_global_ordinal,
                demand.shard_index,
            ),
        )
    )


def wave_count(plan: JobPlan) -> int:
    return max((batch.wave_index for batch in plan.batches), default=-1) + 1


@dataclass(frozen=True)
class TruthBatch:
    batch_id: str
    batch_key: str
    batch_digest: str
    sample_digests: tuple[str, ...]
    sample_refs: tuple[dict[str, Any], ...]
    source_shards: tuple[int, ...]
    worker_id: int
    epoch: int
    local_index: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "batch_key": self.batch_key,
            "batch_digest": self.batch_digest,
            "sample_digests": list(self.sample_digests),
            "sample_refs": list(self.sample_refs),
            "source_shards": list(self.source_shards),
            "worker_id": self.worker_id,
            "epoch": self.epoch,
            "local_index": self.local_index,
        }


def truth_batch(workload: Workload, plan: JobPlan, batch: BatchPlan) -> TruthBatch:
    materialize = batch_digest_and_digests(
        global_seed=workload.global_seed,
        dataset=plan.dataset,
        dataset_fingerprint_value=plan.dataset_fingerprint,
    )
    digest, digests, _ = materialize(batch)
    refs = tuple(
        {
            "dataset": sample.dataset,
            "sample_index": sample.sample_index,
            "shard_index": sample.shard_index,
            "offset_in_shard": sample.offset_in_shard,
        }
        for sample in batch.samples
    )
    return TruthBatch(
        batch_id=batch.batch_id,
        batch_key=batch.batch_key,
        batch_digest=digest,
        sample_digests=tuple(digests),
        sample_refs=refs,
        source_shards=batch.source_shards,
        worker_id=batch.worker_id,
        epoch=batch.epoch,
        local_index=batch.local_index,
    )


def serial_truth(workload: Workload, plan: JobPlan) -> dict[str, TruthBatch]:
    """Compute the unique truth in one process, one batch at a time."""

    result: dict[str, TruthBatch] = {}
    for batch in sorted(plan.batches, key=lambda item: item.batch_id):
        result[batch.batch_id] = truth_batch(workload, plan, batch)
    return result


def sample_owner_map(plan: JobPlan) -> dict[tuple[str, int], int]:
    owners: dict[tuple[str, int], int] = {}
    for batch in plan.batches:
        for sample in batch.samples:
            key = (sample.dataset, sample.sample_index)
            owners.setdefault(key, batch.worker_id)
    return owners


def cross_worker_duplicates(plan: JobPlan) -> list[dict[str, Any]]:
    owners: dict[tuple[str, int], set[int]] = {}
    for batch in plan.batches:
        for sample in batch.samples:
            owners.setdefault((sample.dataset, sample.sample_index), set()).add(
                batch.worker_id
            )
    return [
        {
            "dataset": dataset,
            "sample_index": sample_index,
            "workers": sorted(worker_ids),
        }
        for (dataset, sample_index), worker_ids in sorted(owners.items())
        if len(worker_ids) > 1
    ]


def sample_digest_lookup(
    workload: Workload, plan: JobPlan, sample: SampleRef
) -> str:
    return sample_digest(
        workload.global_seed,
        plan.dataset.name,
        plan.dataset_fingerprint,
        sample.sample_index,
        plan.dataset.feature_dim,
        plan.dataset.sample_bytes,
    )
