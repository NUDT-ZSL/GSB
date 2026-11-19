from __future__ import annotations

from pathlib import Path

from qconv.demo import run_budget_conflict_demo, run_fault_attribution_demo
from qconv.service import QConvService


def test_demo_scenarios_cover_conflict_and_fault(tmp_path: Path) -> None:
    service = QConvService(tmp_path / "snapshots", tmp_path / "cache")
    conflict = run_budget_conflict_demo(service)
    codes = {issue["code"] for issue in conflict["business_errors"]}
    assert {
        "E_MAC_BUDGET",
        "E_MEMORY_LIMIT",
        "E_WORKER_SLOTS",
    }.issubset(codes)
    assert conflict["self_check"]["ok"]
    assert conflict["snapshot"]["generation"] == 1
    modes = {
        plan["job_id"]: plan["accumulator"]["merge_mode"]
        for plan in conflict["plan"]["admitted"]
    }
    assert "float32_chunk_reduce" in modes.values()

    fault = run_fault_attribution_demo(service)
    assert fault["status"] == "partial_failure"
    assert fault["code"] == "E_INJECTED_FAULT"
    assert fault["self_check"]["ok"]
    assert fault["snapshot"]["generation"] == 2
    error = fault["execution"]["worker_errors"][0]
    assert error["chunk_id"] == 0
    assert error["stage"] == "fixed_mac"
    assert fault["execution"]["completed_job_ids"]
    assert fault["execution"]["failed_job_ids"] == [error["job_id"]]

