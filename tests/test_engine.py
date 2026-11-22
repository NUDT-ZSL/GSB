from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from conftest import ROOT, cli
from fieldengine import engine
from fieldengine.execution import comparison_payload, execute_shard, finalize_results
from fieldengine.model import load_experiment
from fieldengine.planning import build_plan


def test_basic_run_and_verify(tmp_path: Path) -> None:
    db = tmp_path / "basic.sqlite3"
    result = engine.run(ROOT / "samples/basic/experiment.json", db)
    assert result["ok"] is True
    assert result["run"]["status"] == "COMPLETE"
    assert result["shard_counts"] == {"COMMITTED": 12}
    assert result["results"] == {"group": 4, "window": 8}
    verified = engine.verify(ROOT / "samples/basic/experiment.json", db)
    assert verified["ok"] is True
    assert verified["groups_compared"] == 4
    assert verified["windows_compared"] == 8
    status = engine.status(db)
    assert status["shard_watermark"]["counts"] == {"COMMITTED": 12}
    assert status["budget"]["reserved_bytes"] == 0
    assert status["budget"]["peak_reserved_bytes"] > 0
    assert status["window_buffer"]["entries"] > 0
    estimate = status["shard_watermark"]["shards"][0]["estimate"]
    assert estimate["observations"]["column_types"]
    assert estimate["observations"]["row_count"] > 0
    assert estimate["observations"]["window_depth"] >= 1


def test_sharding_is_deterministic() -> None:
    experiment = load_experiment(ROOT / "samples/basic/experiment.json")
    first = build_plan(experiment)
    second = build_plan(load_experiment(ROOT / "samples/basic/experiment.json"))
    assert [(s.shard_id, s.shard_digest, s.row_start, s.row_end) for s in first.shard_plans] == [
        (s.shard_id, s.shard_digest, s.row_start, s.row_end) for s in second.shard_plans
    ]
    assert first.plan_digest == second.plan_digest


def test_adaptive_granularity_does_not_change_results() -> None:
    def compute(name: str) -> tuple[str, int, list[str], list[str]]:
        experiment = load_experiment(ROOT / f"samples/{name}/experiment.json")
        plan = build_plan(experiment)
        partials = [execute_shard(experiment, plan, shard) for shard in plan.shard_plans]
        groups, windows = finalize_results(partials, experiment, plan)
        return (
            plan.mode,
            len(plan.shard_plans),
            [comparison_payload("group", item) for item in groups],
            [comparison_payload("window", item) for item in windows],
        )

    coarse_mode, coarse_shards, coarse_groups, coarse_windows = compute("adaptive")
    fine_mode, fine_shards, fine_groups, fine_windows = compute("adaptive_fine")
    assert coarse_mode == "coarse"
    assert fine_mode == "fine"
    assert coarse_shards < fine_shards
    assert coarse_groups == fine_groups
    assert coarse_windows == fine_windows
    adaptive = load_experiment(ROOT / "samples/adaptive/experiment.json")
    adaptive_plan = build_plan(adaptive)
    assert adaptive_plan.mode_decision["triggers"]
    assert adaptive_plan.mode_decision["rejected_mode"] == "fine"
    assert adaptive_plan.mode_decision["rejected_reason"]


def test_anomaly_and_fixed_overflow_diagnostics(tmp_path: Path) -> None:
    db = tmp_path / "anomalies.sqlite3"
    result = engine.run(ROOT / "samples/anomalies/experiment.json", db)
    assert result["ok"] is True
    verified = engine.verify(ROOT / "samples/anomalies/experiment.json", db)
    assert verified["ok"] is True
    assert verified["fixed_overflow_observations"] > 0
    status = engine.status(db)
    assert len(status["results"]) == 12


def test_precision_sensitive_evidence_is_stable(tmp_path: Path) -> None:
    db = tmp_path / "precision.sqlite3"
    experiment = load_experiment(ROOT / "samples/precision/experiment.json")
    plan = build_plan(experiment)
    partials_forward = [execute_shard(experiment, plan, shard) for shard in plan.shard_plans]
    partials_reverse = [execute_shard(experiment, plan, shard) for shard in reversed(plan.shard_plans)]
    groups_forward, _ = finalize_results(partials_forward, experiment, plan)
    groups_reverse, _ = finalize_results(partials_reverse, experiment, plan)
    forward = [comparison_payload("group", item) for item in groups_forward]
    reverse = [comparison_payload("group", item) for item in groups_reverse]
    assert forward == reverse
    sensitive = [
        item for item in groups_forward if item["metrics"]["amount_sum"].get("precision_sensitive")
    ]
    assert len(sensitive) == 2
    evidence = sensitive[0]["metrics"]["amount_sum"]["precision_evidence"][0]
    assert "shards" in evidence["sources"]
    assert "batches" in evidence["sources"]
    assert "conversion_steps" in evidence["sources"]
    result = engine.run(ROOT / "samples/precision/experiment.json", db)
    assert result["ok"] is True
    assert engine.verify(ROOT / "samples/precision/experiment.json", db)["ok"] is True


def test_cli_status_json_is_stably_sorted(tmp_path: Path) -> None:
    db = tmp_path / "cli.sqlite3"
    run = cli(
        "run",
        "--experiment",
        str(ROOT / "samples/basic/experiment.json"),
        "--db",
        str(db),
        cwd=tmp_path,
    )
    assert run.returncode == 0
    first = cli("status", "--db", str(db), cwd=tmp_path)
    second = cli("status", "--db", str(db), cwd=tmp_path)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout


def test_verify_reports_first_differing_group_with_sources(tmp_path: Path) -> None:
    db = tmp_path / "corrupt.sqlite3"
    experiment = ROOT / "samples/basic/experiment.json"
    assert engine.run(experiment, db)["ok"] is True
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT result_key, result_json FROM result WHERE kind = 'group' ORDER BY ordinal LIMIT 1"
    ).fetchone()
    payload = json.loads(row["result_json"])
    payload["metrics"]["amount_sum"]["value"] = 999.0
    payload["metrics"]["amount_sum"]["value_bits"] = "deadbeefdeadbeef"
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        "UPDATE result SET result_json = ?, result_digest = ? WHERE kind = 'group' AND result_key = ?",
        (encoded, hashlib.sha256(encoded.encode()).hexdigest(), row["result_key"]),
    )
    connection.commit()
    connection.close()
    verified = engine.verify(experiment, db)
    assert verified["ok"] is False
    assert verified["code"] == "E_VERIFY_FAILED"
    first_diff = verified["first_diff"]
    assert first_diff["kind"] == "group"
    assert first_diff["identity"] == row["result_key"]
    assert first_diff["metric"] == "amount_sum"
    assert first_diff["sources"]["persisted"]["shards"]
