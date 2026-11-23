from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

import graphstore.scheduler as scheduler
import graphstore.traversal as traversal_module
from graphstore import GraphStore
from graphstore.errors import ErrorCode, GraphStoreError
from graphstore.models import TraversalRequest
from graphstore.snapshot import load_snapshots
from graphstore.merge import merge_snapshots
from graphstore.index import build_index
from graphstore.traversal import (
    first_reach_difference,
    traverse_start_fast,
    traverse_start_truth,
)


def _index(sample_paths):
    v1, v2, package = load_snapshots(
        sample_paths["v1"],
        sample_paths["v2"],
        logical_clock_ms=sample_paths["logical_clock_ms"],
    )
    return build_index(
        merge_snapshots(v1, v2, package), index_version="idx-test"
    )


def _state_store(tmp_path: Path, sample_paths) -> GraphStore:
    store = GraphStore(tmp_path / "state.sqlite")
    store.merge(
        v1_path=sample_paths["v1"],
        v2_path=sample_paths["v2"],
        logical_clock_ms=sample_paths["logical_clock_ms"],
    )
    return store


def test_one_and_two_hop_bfs_and_filters(sample_paths):
    index = _index(sample_paths)
    one = TraversalRequest.create(
        mode="reach", starts=["n1"], max_depth=1, edge_labels=["reads"]
    )
    result = traverse_start_fast(index, "n1", one)
    assert result["ordered_ids"] == ["n1", "n4"]
    assert result["layers"][1]["nodes"][0]["incoming"]["label"] == "reads"

    two = TraversalRequest.create(mode="reach", starts=["n1"], max_depth=2)
    result = traverse_start_fast(index, "n1", two)
    assert result["ordered_ids"] == ["n1", "n2", "n4", "n6", "n5", "n7", "n9"]
    assert "n3" not in result["ordered_ids"]

    filtered = TraversalRequest.create(
        mode="reach",
        starts=["n1"],
        max_depth=2,
        min_edge_weight=1.5,
    )
    assert traverse_start_fast(index, "n1", filtered)["ordered_ids"] == [
        "n1",
        "n4",
        "n5",
        "n7",
    ]


def test_parallel_and_truth_paths_match(sample_paths):
    index = _index(sample_paths)
    request = TraversalRequest.create(
        mode="reach",
        starts=["n1", "n2", "n4", "n7"],
        max_depth=2,
    )
    for start in request.starts:
        fast = traverse_start_fast(index, start, request)
        truth = traverse_start_truth(index, start, request)
        assert first_reach_difference(fast, truth) is None
        assert fast == truth


def test_first_difference_reports_depth_node_and_source(sample_paths):
    index = _index(sample_paths)
    request = TraversalRequest.create(mode="reach", starts=["n1"])
    truth = traverse_start_truth(index, "n1", request)
    parallel = traverse_start_truth(index, "n1", request)
    parallel["layers"][1]["nodes"][0]["id"] = "nX"
    difference = first_reach_difference(parallel, truth)
    assert difference["depth"] == 1
    assert difference["parallel_node"] == "nX"
    assert difference["truth_node"] == "n2"
    assert difference["truth_source"] == "v2:s0001"


def test_exact_prefix_and_business_errors(sample_paths):
    index = _index(sample_paths)
    exact = TraversalRequest.create(mode="exact", exact_node="n1")
    assert traverse_start_fast(index, "n1", TraversalRequest.create(
        mode="reach", starts=["n1"]
    ))["ordered_ids"][0] == "n1"
    assert index.prefix("person/") and all(
        node.label.startswith("person/") for node in index.prefix("person/")
    )
    with pytest.raises(GraphStoreError) as caught:
        index.exact("n3")
    assert caught.value.code == ErrorCode.NODE_NOT_FOUND
    with pytest.raises(GraphStoreError) as caught:
        TraversalRequest.create(mode="reach", starts=["n1"], max_depth=0)
    assert caught.value.code == ErrorCode.INVALID_DEPTH
    with pytest.raises(GraphStoreError) as caught:
        TraversalRequest.create(
            mode="reach", starts=["n1"], edge_labels=["bad label!"]
        )
    assert caught.value.code == ErrorCode.INVALID_EDGE_LABEL
    assert exact.mode == "exact"


def test_budget_abandons_remaining_batches_and_keeps_classification(
    tmp_path, sample_paths
):
    store = _state_store(tmp_path, sample_paths)
    request = TraversalRequest.create(
        mode="reach",
        starts=["n1", "n2", "n4", "n7"],
        max_depth=2,
        max_parallel_tasks=2,
        memory_budget_entries=20,
        batch_size=1,
    )
    output = store.traverse(request)
    outcomes = output["result"]["outcomes"]
    assert outcomes == {
        "abandoned": 3,
        "not_started": 0,
        "started_in_progress": 0,
        "started_succeeded": 1,
        "started_failed": 0,
    }
    assert len(output["result"]["abandoned"]) == 3
    assert all(
        "memory_budget_exhausted" in item["reason"]
        for item in output["result"]["abandoned"]
    )
    assert output["status"]["traversal"]["watermark"] == 4


def test_max_parallel_tasks_is_enforced(tmp_path, sample_paths, monkeypatch):
    store = _state_store(tmp_path, sample_paths)
    active = 0
    maximum = 0
    lock = threading.Lock()
    original = scheduler._execute_plan

    def wrapped(index, request, plan):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.04)
            return original(index, request, plan)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(scheduler, "_execute_plan", wrapped)
    request = TraversalRequest.create(
        mode="reach",
        starts=["n1", "n2", "n4", "n7"],
        max_parallel_tasks=2,
        memory_budget_entries=1000,
        batch_size=1,
    )
    output = store.traverse(request)
    assert output["result"]["outcomes"]["started_succeeded"] == 4
    assert maximum == 2


def test_started_failure_is_distinct_from_abandon(
    tmp_path, sample_paths, monkeypatch
):
    store = _state_store(tmp_path, sample_paths)

    def fail(index, request, plan):
        raise RuntimeError("injected worker failure")

    monkeypatch.setattr(scheduler, "_execute_plan", fail)
    request = TraversalRequest.create(
        mode="reach",
        starts=["n1", "n2"],
        max_parallel_tasks=1,
        memory_budget_entries=1000,
        batch_size=1,
    )
    output = store.traverse(request)
    outcomes = output["result"]["outcomes"]
    assert outcomes["started_failed"] == 2
    assert outcomes["abandoned"] == 0
    assert all(
        item["result"]["error"]["code"] == ErrorCode.BATCH_FAILED
        for item in output["result"]["failures"]
    )


def test_truth_mismatch_reports_first_difference(
    tmp_path, sample_paths, monkeypatch
):
    store = _state_store(tmp_path, sample_paths)
    original = traversal_module.traverse_start_fast

    def corrupt(index, start, request):
        result = original(index, start, request)
        if start == "n1":
            result["layers"][1]["nodes"][0]["id"] = "nX"
        return result

    monkeypatch.setattr(traversal_module, "traverse_start_fast", corrupt)
    output = store.traverse(
        TraversalRequest.create(
            mode="reach",
            starts=["n1"],
            max_depth=2,
            max_parallel_tasks=1,
            memory_budget_entries=100,
            batch_size=1,
        )
    )
    failure = output["result"]["failures"][0]["result"]["error"]
    assert failure["code"] == ErrorCode.TRAVERSAL_MISMATCH
    difference = failure["details"]["first_difference"]
    assert difference["depth"] == 1
    assert difference["parallel_node"] == "nX"
    assert difference["truth_node"] == "n2"
    assert difference["truth_source"] == "v2:s0001"


def test_status_before_start_reports_not_started(tmp_path, sample_paths):
    store = _state_store(tmp_path, sample_paths)
    index, _meta = store.state.load_index()
    request = TraversalRequest.create(
        mode="reach",
        starts=["n1", "n2"],
        max_parallel_tasks=1,
        memory_budget_entries=1000,
        batch_size=1,
    )
    from graphstore.scheduler import make_run_id, plan_batches

    run_id = make_run_id(index, request)
    store.state.ensure_run(
        run_id=run_id,
        index_version=index.index_version,
        request=request,
        plans=plan_batches(index, request),
    )
    status = store.status(run_id)
    assert status["traversal"]["outcomes"]["not_started"] == 2
    assert status["traversal"]["watermark"] == 0

