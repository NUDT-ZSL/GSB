from __future__ import annotations

import json
from pathlib import Path

from conftest import ROOT, cli, copy_experiment, load_json, write_json
from fieldengine import engine


def test_memory_budget_abandons_before_start_and_commits_no_result(tmp_path: Path) -> None:
    db = tmp_path / "budget.sqlite3"
    result = engine.run(ROOT / "samples/budget/experiment.json", db)
    assert result["ok"] is False
    assert result["run"]["status"] == "INCOMPLETE"
    assert result["run"]["error_code"] == "E_RUN_ABANDONED"
    assert result["shard_counts"] == {"ABANDONED": 12}
    status = engine.status(db)
    assert status["results"] == []
    assert status["budget"]["reserved_bytes"] == 0
    assert all(item["bytes"] == 0 for item in status["budget"]["events"])
    assert {item["reason"] for item in status["budget"]["events"]} == {
        "MEMORY_BUDGET_PER_TASK"
    }
    verify = engine.verify(ROOT / "samples/budget/experiment.json", db)
    assert verify["ok"] is False
    assert verify["code"] == "E_RUN_INCOMPLETE"


def test_injected_started_failure_is_distinct_from_abandon(tmp_path: Path) -> None:
    experiment = copy_experiment(tmp_path, "basic")
    injections_path = experiment.parent / "injections.json"
    write_json(
        injections_path,
        {
            "version": 1,
            "injections": [
                {
                    "id": "fail-one-shard",
                    "action": "fail",
                    "trigger": {"shard_id": "b000000-s0000"},
                    "error_code": "E_TEST_SHARD_FAILURE",
                    "once": True,
                }
            ],
        },
    )
    db = tmp_path / "failed.sqlite3"
    result = engine.run(experiment, db)
    assert result["ok"] is False
    assert result["run"]["status"] == "INCOMPLETE"
    status = engine.status(db)
    assert status["shard_watermark"]["counts"].get("FAILED") == 1
    failed = [
        item
        for item in status["shard_watermark"]["shards"]
        if item["status"] == "FAILED"
    ][0]
    assert failed["error_code"] == "E_TEST_SHARD_FAILURE"
    assert status["results"] == []


def test_two_store_instances_do_not_oversell_active_leases(tmp_path: Path) -> None:
    from fieldengine.model import load_experiment
    from fieldengine.planning import build_plan
    from fieldengine.store import Store

    experiment = load_experiment(ROOT / "samples/basic/experiment.json")
    plan = build_plan(experiment)
    db = tmp_path / "lease.sqlite3"
    first = Store(db)
    first.initialize(experiment, plan)
    claimed_a = first.claim_shards(experiment, plan, limit=experiment.config.parallel_limit)
    assert len(claimed_a) == experiment.config.parallel_limit
    second = Store(db)
    claimed_b = second.claim_shards(
        experiment, plan, limit=experiment.config.parallel_limit
    )
    assert claimed_b == []
    status = second.status()
    assert status["budget"]["active_leases"] == experiment.config.parallel_limit
    assert status["budget"]["reserved_bytes"] <= experiment.config.total_memory_budget_bytes
    first.close()
    second.close()
