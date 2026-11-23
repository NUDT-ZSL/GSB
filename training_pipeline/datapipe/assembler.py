"""Batch assembly and the single-process serial source of truth.

Worker -> shard assignment is a stable round-robin over the fixed shard order,
so the same shard is always owned by the same worker and no two workers ever
overlap.  Samples are then flattened in deterministic
``(dataset, shard, sample)`` order and chopped into fixed-size batches, which
guarantees each sample appears exactly once per epoch.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .hashing import combine_digests
from .injection import InjectionEngine
from .model import DatasetSpec, JobSpec, Scenario
from .sharding import SampleRecord, build_shard_manifest, fixed_sample_stream, shard_key
from .store import Store
from .errors import E_BATCH_DIVERGENCE, E_SAMPLE_COVERAGE, fail


@dataclass(frozen=True)
class Batch:
    epoch: int
    batch_index: int
    sample_identities: Tuple[str, ...]
    sources: Tuple[str, ...]
    digest: str


def worker_shard_assignment(job: JobSpec, specs: Dict[str, DatasetSpec]) -> Dict[int, List[str]]:
    """Deterministic, non-overlapping shard assignment across data-parallel workers."""
    assignment: Dict[int, List[str]] = {worker: [] for worker in range(job.data_parallel)}
    ordered_shards: List[str] = []
    for dataset in job.datasets:
        spec = specs[dataset]
        for shard_index in range(spec.shard_count):
            ordered_shards.append(shard_key(dataset, shard_index))
    for position, key in enumerate(ordered_shards):
        assignment[position % job.data_parallel].append(key)
    return assignment


def serial_truth_batches(scenario: Scenario) -> List[Batch]:
    """The single source of truth: single-process serial generation + assembly."""
    specs = {spec.name: spec for spec in scenario.datasets}
    batches: List[Batch] = []
    for epoch in range(scenario.job.epochs):
        stream: List[SampleRecord] = []
        for dataset in scenario.job.datasets:
            stream.extend(fixed_sample_stream(specs[dataset]))
        cursor = 0
        batch_index = 0
        while cursor < len(stream):
            chunk = stream[cursor:cursor + scenario.job.batch_size]
            identities = tuple(sample.identity for sample in chunk)
            sources = tuple(shard_key(sample.dataset, sample.shard_index) for sample in chunk)
            digest = combine_digests(f"epoch{epoch}:batch{batch_index}", identities)
            batches.append(
                Batch(epoch=epoch, batch_index=batch_index, sample_identities=identities,
                      sources=sources, digest=digest)
            )
            cursor += scenario.job.batch_size
            batch_index += 1
    return batches


def verify_coverage(scenario: Scenario, batches: Sequence[Batch]) -> None:
    """Assert every sample appears exactly once in each epoch (no dup/no gap)."""
    specs = {spec.name: spec for spec in scenario.datasets}
    expected_per_epoch: List[str] = []
    for dataset in scenario.job.datasets:
        expected_per_epoch.extend(sample.identity for sample in fixed_sample_stream(specs[dataset]))
    for epoch in range(scenario.job.epochs):
        epoch_batches = [batch for batch in batches if batch.epoch == epoch]
        seen = [identity for batch in epoch_batches for identity in batch.sample_identities]
        if len(seen) != len(set(seen)):
            raise fail(E_SAMPLE_COVERAGE, "duplicate sample detected in epoch", epoch=epoch)
        if sorted(seen) != sorted(expected_per_epoch):
            missing = sorted(set(expected_per_epoch) - set(seen))
            extra = sorted(set(seen) - set(expected_per_epoch))
            raise fail(
                E_SAMPLE_COVERAGE,
                "epoch sample coverage mismatch",
                epoch=epoch,
                missing_count=len(missing),
                extra_count=len(extra),
                missing=missing[:3],
                extra=extra[:3],
            )


def compare_with_truth(scenario: Scenario, actual: Sequence[Batch]) -> None:
    """Compare the parallel path against serial truth, locating first divergence."""
    truth = serial_truth_batches(scenario)
    if len(truth) != len(actual):
        raise fail(
            E_BATCH_DIVERGENCE,
            "batch count differs from serial truth",
            truth_batches=len(truth),
            actual_batches=len(actual),
        )
    for truth_batch, actual_batch in zip(truth, actual):
        if truth_batch.digest == actual_batch.digest:
            continue
        for position, (left, right) in enumerate(
            zip(truth_batch.sample_identities, actual_batch.sample_identities)
        ):
            if left != right:
                raise fail(
                    E_BATCH_DIVERGENCE,
                    "first sample-level divergence against serial truth",
                    epoch=truth_batch.epoch,
                    batch_index=truth_batch.batch_index,
                    sample_position=position,
                    truth_identity=left,
                    actual_identity=right,
                    truth_source=truth_batch.sources[position],
                    actual_source=actual_batch.sources[position],
                )
        raise fail(
            E_BATCH_DIVERGENCE,
            "batch digest divergence with aligned sample length",
            epoch=truth_batch.epoch,
            batch_index=truth_batch.batch_index,
            truth_digest=truth_batch.digest,
            actual_digest=actual_batch.digest,
        )


class Assembler:
    """Assembles durable batches from already-committed shards."""

    def __init__(self, scenario: Scenario, store: Store, injector: InjectionEngine):
        self.scenario = scenario
        self.store = store
        self.injector = injector
        self.specs = {spec.name: spec for spec in scenario.datasets}

    def _epoch_stream(self) -> List[SampleRecord]:
        # Re-derive samples through the same deterministic shard manifests that
        # the prefetch path committed, proving content is keyed only by
        # (dataset, shard, sample) and independent of prefetch ordering.
        stream: List[SampleRecord] = []
        for dataset in self.scenario.job.datasets:
            spec = self.specs[dataset]
            for shard_index in range(spec.shard_count):
                row = self.store.shard_committed(dataset, shard_index, _algebra(spec))
                if row is None:
                    raise fail(
                        E_SAMPLE_COVERAGE,
                        "cannot assemble: shard not committed",
                        dataset=dataset,
                        shard_index=shard_index,
                    )
                manifest = build_shard_manifest(spec, shard_index)
                stream.extend(manifest.samples)
        return stream

    def assemble(self) -> List[Batch]:
        batches: List[Batch] = []
        for epoch in range(self.scenario.job.epochs):
            stream = self._epoch_stream()
            cursor = 0
            batch_index = 0
            while cursor < len(stream):
                chunk = stream[cursor:cursor + self.scenario.job.batch_size]
                identities = tuple(sample.identity for sample in chunk)
                sources = tuple(shard_key(sample.dataset, sample.shard_index) for sample in chunk)
                digest = combine_digests(f"epoch{epoch}:batch{batch_index}", identities)

                self.injector.check("assemble", f"epoch{epoch}#b{batch_index}")
                # Idempotent batch record; a conflicting digest raises E4001.
                self.store.record_batch(epoch, batch_index, digest, len(identities), list(sources))
                batches.append(
                    Batch(epoch=epoch, batch_index=batch_index, sample_identities=identities,
                          sources=sources, digest=digest)
                )
                cursor += self.scenario.job.batch_size
                batch_index += 1
            self.store.set_watermark("assemble_epoch", epoch)
        verify_coverage(self.scenario, batches)
        compare_with_truth(self.scenario, batches)
        return batches


def _algebra(spec: DatasetSpec) -> int:
    from .sharding import cache_algebra

    return cache_algebra(spec)
