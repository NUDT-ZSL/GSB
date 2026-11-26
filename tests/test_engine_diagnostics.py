from __future__ import annotations

from pathlib import Path

from cold_diag.models import PHASE_LABELS_CN
from cold_diag.runner import run_scenario, summarize_result
from cold_diag.sample_data import generate_sample
from cold_diag.utils import canonical_json


def _run(tmp_path: Path, name: str = "state.sqlite"):
    data = generate_sample(tmp_path / "data")
    db = tmp_path / name
    result = run_scenario(data["root"], db_path=db)
    return data, result


def test_state_machine_dispatch_budget_and_precision(tmp_path: Path) -> None:
    _, result = _run(tmp_path)
    summary = summarize_result(result)
    state = result["state"]

    phases = {instance["phase"] for instance in state["instances"].values()}
    for instance in state["instances"].values():
        phases.update(event["to_phase"] for event in instance["history"] if event["to_phase"])
    required = {
        "pending_warmup",
        "warming",
        "ready",
        "busy",
        "recycling",
    }
    assert required <= phases
    assert all(PHASE_LABELS_CN[phase] for phase in required)

    dispatch = {row["seq"]: row for row in summary["dispatch_report"]}
    assert dispatch[1]["instance_id"] == "alpha-1"
    assert dispatch[2]["instance_id"] == "alpha-1"
    assert dispatch[1]["dispatch_clock_ms"] == 100
    assert dispatch[2]["wait_ms"] == 96
    assert dispatch[5]["instance_id"] == "alpha-2"
    assert dispatch[8]["instance_id"] == "delta-3"

    outcomes = {row["candidate_id"]: row for row in summary["candidate_outcomes"]}
    assert outcomes["gamma-1"]["outcome"] == "started"
    assert outcomes["flaky-1"]["outcome"] == "warmup_failed"
    assert outcomes["delta-1"]["outcome"] == "abandoned"
    assert outcomes["delta-1"]["reason"] == "global_cold_start_limit_tail"
    assert outcomes["delta-2"]["outcome"] == "abandoned"
    assert outcomes["delta-2"]["reason"] == "insufficient_logical_budget"
    assert outcomes["flaky-2"]["outcome"] == "not_started_in_budget"
    assert outcomes["flaky-2"]["reason"] == "global_cold_start_limit"

    sensitive = [marker for marker in state["ledger"]["markers"] if marker["sensitive"]]
    assert [(marker["seq_a"], marker["seq_b"]) for marker in sensitive] == [(1, 2)]
    assert sensitive[0]["difference_ms"] == 4
    assert sensitive[0]["resolution"] == "mark_sensitive_keep_priority_then_request_seq"

    for row in summary["dispatch_report"]:
        assert row["status"] == "completed"
        if row["dispatch_clock_ms"] is not None:
            assert row["dispatch_clock_ms"] >= row["arrival_clock_ms"]


def test_repeated_runs_are_byte_identical(tmp_path: Path) -> None:
    data = generate_sample(tmp_path / "data")
    first = run_scenario(data["root"], db_path=tmp_path / "first.sqlite")
    first_bytes = canonical_json(first["state"])
    first["engine"].close()

    second = run_scenario(data["root"], db_path=tmp_path / "second.sqlite")
    second_bytes = canonical_json(second["state"])
    second["engine"].close()

    assert first_bytes == second_bytes


def test_dispatch_never_uses_warming_or_recycling(tmp_path: Path) -> None:
    _, result = _run(tmp_path)
    state = result["state"]
    for call in state["calls"].values():
        if not call.get("instance_id"):
            continue
        instance = state["instances"][call["instance_id"]]
        dispatch_clock = call["dispatch_clock_ms"]
        assert dispatch_clock is not None
        assert dispatch_clock >= instance["ready_at_ms"]
        history = instance["history"]
        dispatch_phases = [
            event["to_phase"]
            for event in history
            if event["clock_ms"] <= dispatch_clock
        ]
        assert "recycling" not in dispatch_phases or dispatch_clock < next(
            event["clock_ms"]
            for event in history
            if event["to_phase"] == "recycling"
        )
