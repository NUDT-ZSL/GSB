from __future__ import annotations

import json
from pathlib import Path

from columnar_engine.engine import Engine

from conftest import SAMPLE, write_json


def test_memory_budget_abandons_before_start(tmp_path: Path, sample_dir) -> None:
    summary = Engine(tmp_path / "engine.db", tmp_path / "var").run_sample(
        sample_dir,
        crash_after_commits=None,
    )
    assert summary.status == "COMPLETE"

    budget_summary = Engine(tmp_path / "budget.db", tmp_path / "budget-var").run_paths(
        sample_dir / "batches.json",
        sample_dir / "plan.json",
        SAMPLE.parent / "budget_abandon" / "config.json",
    )
    assert budget_summary.status == "ABANDONED_PARTIAL"
    assert budget_summary.shard_status_counts == {"ABANDONED": 1}
    assert budget_summary.scheduler["abandoned"] == [
        {"shard_id": 0, "reason": "SHARD_EXCEEDS_SCAN_MEMORY_BUDGET"}
    ]
    assert budget_summary.scheduler["waves"] == []


def test_started_failure_and_not_started_are_distinct(tmp_path: Path, sample_dir) -> None:
    summary = Engine(tmp_path / "engine.db", tmp_path / "var").run_sample(
        sample_dir,
        fail_shard_id=0,
    )
    assert summary.status == "FAILED_PARTIAL"
    assert summary.shard_status_counts["FAILED"] == 1
    assert summary.shard_status_counts["NOT_STARTED"] == 3
    assert {item["reason"] for item in summary.scheduler["not_started"]} == {
        "PEER_SHARD_FAILURE"
    }
    assert summary.scheduler["failed"][0]["code"] == "E_SHARD_FAILURE"
    assert summary.scheduler["abandoned"] == []


def test_failure_after_one_commit_keeps_prior_state(tmp_path: Path, sample_dir) -> None:
    summary = Engine(tmp_path / "engine.db", tmp_path / "var").run_sample(
        sample_dir,
        fail_shard_id=1,
    )
    assert summary.status == "FAILED_PARTIAL"
    assert summary.shard_status_counts.get("COMMITTED", 0) == 1
    snapshot = Engine(tmp_path / "engine.db", tmp_path / "var").snapshot(summary.run_id)
    assert {state["node_id"]: state["watermark"] for state in snapshot["node_states"]} == {
        "window1": 0,
        "agg1": 0,
    }


def test_global_budget_is_checked_independently(tmp_path: Path, sample_dir) -> None:
    config = json.loads((SAMPLE / "config.json").read_text(encoding="utf-8"))
    config["global_memory_budget_mib"] = 0.0000001
    path = write_json(tmp_path / "tight-global.json", config)
    summary = Engine(tmp_path / "engine.db", tmp_path / "var").run_paths(
        sample_dir / "batches.json",
        sample_dir / "plan.json",
        path,
    )
    assert summary.status == "ABANDONED_PARTIAL"
    assert summary.scheduler["abandoned"][0]["reason"] == "GLOBAL_MEMORY_BUDGET_EXHAUSTED"

