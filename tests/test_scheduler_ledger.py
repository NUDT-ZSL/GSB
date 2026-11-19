from __future__ import annotations

import threading
import time

import pytest

from mixedpipe.canonical import canonical_json_bytes
from mixedpipe.errors import (
    E_COMPUTE_BUDGET,
    E_MEMORY_BUDGET,
    E_SLOT_BUDGET,
    BusinessError,
)
from mixedpipe.ledger import BudgetLedger
from mixedpipe.models import PipelineConfig, Task
from mixedpipe.samples import demo_config, demo_tasks
from mixedpipe.scheduler import build_plan


def test_plan_is_reproducible_and_contains_required_rejection() -> None:
    config = demo_config()
    first = build_plan(demo_tasks(), config)
    second = build_plan(demo_tasks(), config)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first["rejection_counts"] == {E_COMPUTE_BUDGET: 1}
    rejected = [
        decision
        for decision in first["decisions"]
        if decision["decision"] == "rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0]["job_id"] == "job-008"
    assert rejected[0]["blocking_constraints"] == [E_COMPUTE_BUDGET]
    assert first["parallel_groups"]
    assert all(
        group["slot_count"] <= config.max_slots
        for group in first["parallel_groups"]
    )


def test_inference_weight_sharing_is_visible_in_decisions() -> None:
    plan = build_plan(demo_tasks(), demo_config())
    decisions = {decision["job_id"]: decision for decision in plan["decisions"]}
    assert decisions["job-002"]["shared_weight_reused"] is False
    assert decisions["job-003"]["shared_weight_reused"] is True
    assert (
        decisions["job-003"]["original_estimate"]["weight_charged_bytes"] == 0
    )


def test_memory_and_slot_rejections_have_structured_codes() -> None:
    task = Task(
        "job-memory",
        "train",
        "big",
        8,
        8,
        64,
        seed=1,
        priority=1,
        steps=2,
    )
    memory_config = PipelineConfig(
        max_slots=1,
        compute_budget=10**12,
        memory_budget_mb=0.000001,
        degradation_enabled=False,
    )
    memory_plan = build_plan([task], memory_config)
    assert memory_plan["decisions"][0]["business_code"] == E_MEMORY_BUDGET

    slot_config = PipelineConfig(
        max_slots=0,
        compute_budget=10**12,
        memory_budget_mb=8,
        degradation_enabled=False,
    )
    slot_plan = build_plan([task], slot_config)
    assert slot_plan["decisions"][0]["business_code"] == E_SLOT_BUDGET
    assert slot_plan["parallel_groups"] == []


def test_degradation_marks_fields_and_returns_resources() -> None:
    task = Task(
        "job-degrade",
        "train",
        "degrade-model",
        8,
        8,
        8,
        seed=5,
        priority=10,
        steps=4,
    )
    config = PipelineConfig(
        max_slots=1,
        compute_budget=10_000,
        memory_budget_mb=8,
        degradation_enabled=True,
        min_batch_size=1,
        min_iterations=1,
        max_degrade_candidates=100,
    )
    plan = build_plan([task], config)
    decision = plan["decisions"][0]
    assert decision["decision"] == "admitted"
    assert decision["degraded"] is True
    assert decision["degrade_category"] == "batch_and_iterations"
    assert decision["degraded_fields"] == {
        "batch_size": {"from": 8, "to": 1},
        "steps": {"from": 4, "to": 2},
    }
    assert decision["degrade_returned"]["compute_units"] > 0
    assert plan["degraded_job_ids"] == ["job-degrade"]


def test_ledger_is_atomic_for_concurrent_readers() -> None:
    config = PipelineConfig(
        max_slots=3,
        compute_budget=20_000_000,
        memory_budget_mb=8,
    )
    plan = build_plan(demo_tasks(), config)
    ledger = BudgetLedger(config)
    ledger.reserve_plan(plan)

    failures: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            snapshot = ledger.snapshot()
            entry_compute = sum(
                entry["effective_compute_units"]
                for entry in snapshot["entries"]
            )
            entry_memory = sum(
                entry["effective_memory_bytes"] for entry in snapshot["entries"]
            )
            if entry_compute != snapshot["totals"]["effective_compute_units"]:
                failures.append("compute total mismatch")
            if entry_memory != snapshot["totals"]["effective_memory_bytes"]:
                failures.append("memory total mismatch")
            if snapshot["state"] not in {"reserved", "settled"}:
                failures.append("partial ledger state")
            time.sleep(0)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in readers:
        thread.start()
    results = [
        {
            "job_id": job_id,
            "status": "error" if job_id == "job-006" else "ok",
        }
        for job_id in plan["admitted_job_ids"]
    ]
    ledger.settle_results(results)
    stop.set()
    for thread in readers:
        thread.join(timeout=2)

    assert failures == []
    ledger.assert_invariants()
    final = ledger.snapshot()
    assert final["state"] == "settled"
    assert (
        final["totals"]["consumed_compute_units"]
        + final["totals"]["released_compute_units"]
        == final["totals"]["effective_compute_units"]
    )
    assert (
        final["totals"]["net_committed_compute_units"]
        == final["totals"]["consumed_compute_units"]
    )
    released = next(
        entry for entry in final["entries"] if entry["job_id"] == "job-006"
    )
    assert released["status"] == "released"
    assert released["released_compute_units"] > 0


def test_ledger_rejects_one_to_one_mismatch() -> None:
    config = PipelineConfig(
        max_slots=1,
        compute_budget=1_000_000,
        memory_budget_mb=8,
    )
    plan = build_plan([demo_tasks()[0]], config)
    ledger = BudgetLedger(config)
    ledger.reserve_plan(plan)
    with pytest.raises(BusinessError) as excinfo:
        ledger.settle_results([])
    assert excinfo.value.code == "E_VALIDATION_FAILED"
