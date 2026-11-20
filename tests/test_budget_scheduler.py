from __future__ import annotations

import copy
import time

import pytest

from shardpipe import scheduler as scheduler_module
from shardpipe.scheduler import run_schedule, status_report
from shardpipe.storage import StateStore
from shardpipe.verification import verify_run
from shardpipe.workload import parse_workload

from .conftest import one_dataset_document, store_with_document


def test_memory_budget_abandons_unstarted_shards(tmp_path):
    document = one_dataset_document(
        sample_count=4,
        sample_bytes=400 * 1024,
        shard_size=2,
        batch_size=1,
        time_budget_ms=1000,
        bytes_per_ms=1024 * 1024,
    )
    store = store_with_document(tmp_path / "state", document)
    workload = store.get_workload()
    result = run_schedule(
        store=store,
        workload=workload,
        job=workload.jobs[0],
        run_id="memory-run",
    )
    assert result["status"] == "incomplete"
    assert result["result"]["memory_bytes"] == 800 * 1024
    assert result["result"]["memory_peak_bytes"] == 800 * 1024
    assert [item["shard_index"] for item in result["result"]["abandoned_shards"]] == [
        1
    ]
    assert result["result"]["committed_batches"] == 2
    status = status_report(store, "memory-run")
    assert status["datasets"][0]["abandoned_shards"][0]["reason"] == (
        "memory_budget_exhausted"
    )
    verified = verify_run(store, run_id="memory-run", allow_incomplete=True)
    assert verified["ok"]


def test_logical_time_budget_marks_not_started(tmp_path):
    document = one_dataset_document(
        sample_count=4,
        sample_bytes=64 * 1024,
        shard_size=2,
        batch_size=1,
        time_budget_ms=2,
        bytes_per_ms=1,
        memory_mib=8,
    )
    store = store_with_document(tmp_path / "state", document)
    workload = store.get_workload()
    result = run_schedule(
        store=store,
        workload=workload,
        job=workload.jobs[0],
        run_id="time-run",
    )
    assert result["status"] == "incomplete"
    assert result["result"]["not_started_shards"]
    rounds = store.get_rounds("time-run")
    assert all(
        row["logical_end_ms"] - row["logical_start_ms"]
        <= row["state"]["budget_ms"]
        for row in rounds
    )
    assert verify_run(store, run_id="time-run", allow_incomplete=True)["ok"]


def test_concurrency_limit_controls_logical_start_times(tmp_path):
    document = one_dataset_document(
        sample_count=8,
        sample_bytes=1024,
        shard_size=2,
        batch_size=4,
        prefetch_depth=1,
        time_budget_ms=20,
        base_ms=1,
        bytes_per_ms=4096,
        memory_mib=8,
    )
    store = store_with_document(tmp_path / "state", document)
    workload = store.get_workload()
    result = run_schedule(
        store=store,
        workload=workload,
        job=workload.jobs[0],
        run_id="concurrency-run",
    )
    assert result["status"] == "complete"
    rounds = store.get_rounds("concurrency-run")
    first_round_candidates = rounds[0]["state"]["outcomes"]
    assert len(first_round_candidates) == 2
    started = sorted(
        (item["start_ms"], item["end_ms"])
        for item in first_round_candidates
        if item["outcome"] == "generated"
    )
    assert started == [(0, 2), (2, 4)]


def test_same_clock_inputs_produce_identical_decisions(tmp_path):
    document = one_dataset_document(
        sample_count=16,
        sample_bytes=2048,
        shard_size=2,
        batch_size=4,
        prefetch_depth=1,
        time_budget_ms=16,
        base_ms=1,
        bytes_per_ms=4096,
        memory_mib=8,
    )
    decisions = []
    for index in range(2):
        state_dir = tmp_path / f"state-{index}"
        store = store_with_document(state_dir, copy.deepcopy(document))
        workload = store.get_workload()
        result = run_schedule(
            store=store,
            workload=workload,
            job=workload.jobs[0],
            run_id=f"deterministic-{index}",
        )
        rounds = store.get_rounds(f"deterministic-{index}")
        decisions.append(
            {
                "status": result["status"],
                "clock": result["result"]["clock_ms"],
                "memory": result["result"]["memory_bytes"],
                "outcomes": [
                    [
                        (
                            item["shard_index"],
                            item["outcome"],
                            item["start_ms"],
                            item["end_ms"],
                        )
                        for item in row["state"]["outcomes"]
                    ]
                    for row in rounds
                ],
            }
        )
    assert decisions[0] == decisions[1]


def test_actual_wait_timeout_requires_clock_and_wait(tmp_path, monkeypatch):
    document = one_dataset_document(
        sample_count=2,
        sample_bytes=64,
        shard_size=2,
        batch_size=1,
        time_budget_ms=10,
    )
    store = store_with_document(tmp_path / "state", document)
    workload = store.get_workload()

    # Retain the original implementation because monkeypatching replaces it.
    original = scheduler_module._generate_one
    monkeypatch.setattr(
        scheduler_module,
        "_generate_one",
        lambda **kwargs: (time.sleep(0.05), original(**kwargs))[1],
    )
    result = run_schedule(
        store=store,
        workload=workload,
        job=workload.jobs[0],
        run_id="timeout-run",
    )
    assert result["status"] == "incomplete"
    assert result["result"]["actual_timeouts"]
    assert result["result"]["failures"][0]["reason"] == (
        "logical_and_actual_wait_timeout"
    )
    assert result["result"]["memory_bytes"] == 0
    assert result["result"]["memory_peak_bytes"] == 128
    assert verify_run(store, run_id="timeout-run", allow_incomplete=True)["ok"]
