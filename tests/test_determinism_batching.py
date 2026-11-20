from __future__ import annotations

import numpy as np

from shardpipe.batching import (
    build_job_plan,
    cross_worker_duplicates,
    sample_owner_map,
    serial_truth,
)
from shardpipe.determinism import (
    dataset_fingerprint,
    generate_shard,
    sample_record,
    shard_manifest,
)
from shardpipe.workload import parse_workload


def test_shard_generation_is_bitwise_deterministic(sample_document):
    workload = parse_workload(sample_document)
    dataset = workload.dataset_map()["alpha"]
    fingerprint = dataset_fingerprint(workload.global_seed, dataset)
    first = generate_shard(
        global_seed=workload.global_seed,
        dataset=dataset,
        dataset_fingerprint_value=fingerprint,
        shard_index=2,
    )
    second = generate_shard(
        global_seed=workload.global_seed,
        dataset=dataset,
        dataset_fingerprint_value=fingerprint,
        shard_index=2,
    )
    assert first == second
    assert len(first[0]) == dataset.shard_bytes
    manifest = shard_manifest(
        global_seed=workload.global_seed,
        dataset=dataset,
        dataset_fingerprint_value=fingerprint,
        shard_index=2,
    )
    assert manifest["digest"] == first[1]
    assert manifest["sample_start"] == 32
    assert manifest["sample_end"] == 48


def test_sample_records_expose_deterministic_float_features(sample_document):
    workload = parse_workload(sample_document)
    dataset = workload.dataset_map()["beta"]
    fingerprint = dataset_fingerprint(workload.global_seed, dataset)
    record = sample_record(
        workload.global_seed,
        dataset.name,
        fingerprint,
        5,
        dataset.feature_dim,
        dataset.sample_bytes,
    )
    features = np.frombuffer(record[: dataset.feature_dim * 4], dtype="<f4")
    assert features.shape == (dataset.feature_dim,)
    assert np.isfinite(features).all()
    assert record == sample_record(
        workload.global_seed,
        dataset.name,
        fingerprint,
        5,
        dataset.feature_dim,
        dataset.sample_bytes,
    )


def test_reuse_plan_has_stable_order_and_no_duplicate_samples(sample_document):
    workload = parse_workload(sample_document)
    job = workload.job_map()["train-alpha"]
    first = build_job_plan(workload, job)
    second = build_job_plan(workload, job)
    assert [batch.batch_id for batch in first.batches] == [
        batch.batch_id for batch in second.batches
    ]
    assert cross_worker_duplicates(first) == []
    owners = sample_owner_map(first)
    assert len(owners) == first.dataset.sample_count
    assert all(len(batch.samples) == job.batch_size for batch in first.batches)
    assert all(batch.source_shards for batch in first.batches)
    assert any(
        len(worker_ids) > 1 for worker_ids in first.shard_to_workers.values()
    )
    truth = serial_truth(workload, first)
    assert len(truth) == len(first.batches)
    assert len({item.batch_digest for item in truth.values()}) == len(truth)


def test_exclusive_plan_never_shares_a_shard(sample_document):
    workload = parse_workload(sample_document)
    job = workload.job_map()["train-beta"]
    plan = build_job_plan(workload, job)
    assert all(
        len(worker_ids) == 1 for worker_ids in plan.shard_to_workers.values()
    )
    worker_shards = [
        set(worker.shard_indices) for worker in plan.workers
    ]
    assert not (worker_shards[0] & worker_shards[1])
    assert worker_shards[0] | worker_shards[1] == set(
        range(plan.dataset.shard_count)
    )
    assert cross_worker_duplicates(plan) == []
    assert len(sample_owner_map(plan)) == plan.dataset.sample_count


def test_batch_sample_refs_match_fixed_shard_boundaries(sample_document):
    workload = parse_workload(sample_document)
    job = workload.job_map()["train-alpha"]
    plan = build_job_plan(workload, job)
    for batch in plan.batches:
        assert [sample.sample_index for sample in batch.samples] == list(
            range(
                batch.samples[0].sample_index,
                batch.samples[-1].sample_index + 1,
            )
        )
        for sample in batch.samples:
            assert sample.shard_index == (
                sample.sample_index // plan.dataset.shard_size
            )
            assert sample.offset_in_shard == (
                sample.sample_index % plan.dataset.shard_size
            )
