from __future__ import annotations

import json
from pathlib import Path

import pytest

from mixedpipe.canonical import atomic_write_json, envelope_checksum
from mixedpipe.errors import (
    E_COMPUTE_BUDGET,
    E_INCONSISTENT,
    E_INJECTED_FAULT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_VERSION_CONFLICT,
    BusinessError,
)
from mixedpipe.models import load_config, load_manifest
from mixedpipe.pipeline import Pipeline
from mixedpipe.samples import demo_config, demo_tasks
from mixedpipe.snapshots import SNAPSHOT_SCHEMA_VERSION
from mixedpipe.validator import Validator
from mixedpipe.web import build_state


def test_demo_pipeline_validation_and_version_increment(demo_root: Path) -> None:
    manifest = demo_root / "data" / "jobs.jsonl"
    config = demo_root / "configs" / "demo.json"
    first = Pipeline(demo_root).run(load_manifest(manifest), load_config(config))
    assert first["plan"]["rejection_counts"] == {E_COMPUTE_BUDGET: 1}
    assert [
        result["job_id"]
        for result in first["results"]
        if result["status"] == "error"
    ] == ["job-006"]
    fault = next(result for result in first["results"] if result["job_id"] == "job-006")
    assert fault["business_code"] == E_INJECTED_FAULT
    assert fault["stage"] == "score"
    validation = Validator(demo_root, manifest, config).validate()
    assert validation["ok"] is True
    assert validation["jobs_verified"] == 6
    assert validation["errors_attributed"] == 1
    assert len(set(validation["snapshot_versions"].values())) == 1

    second = Pipeline(demo_root).run(load_manifest(manifest), load_config(config))
    assert second["stage_version"] == first["stage_version"] + 1
    assert Validator(demo_root, manifest, config).validate()["ok"] is True


def test_degraded_task_is_executed_and_verified(
    demo_root: Path,
) -> None:
    manifest = demo_root / "data" / "degraded.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "job_id": "job-degrade",
                "job_kind": "train",
                "model_tag": "degrade-model",
                "batch_size": 8,
                "seq_len": 8,
                "steps": 4,
                "feature_dim": 8,
                "seed": 5,
                "priority": 10,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config_path = demo_root / "configs" / "degraded.json"
    config_values = demo_config().to_dict()
    config_values.update(
        {
            "compute_budget": 10_000,
            "min_batch_size": 1,
            "min_iterations": 1,
            "max_degrade_candidates": 100,
        }
    )
    config_path.write_text(json.dumps(config_values), encoding="utf-8")

    run = Pipeline(demo_root).run(
        load_manifest(manifest),
        load_config(config_path),
    )
    decision = run["plan"]["decisions"][0]
    assert decision["degraded"] is True
    assert decision["degrade_category"] == "batch_and_iterations"
    assert run["results"][0]["effective_task"]["batch_size"] == 1
    assert run["results"][0]["effective_task"]["steps"] == 2
    validation = Validator(demo_root, manifest, config_path).validate()
    assert validation["ok"] is True
    assert validation["degraded_jobs_verified"] == 1
    assert validation["degraded_checks"][0]["matches"] is True


def test_snapshot_corruption_is_reported_by_validator(demo_root: Path) -> None:
    manifest = demo_root / "data" / "jobs.jsonl"
    config = demo_root / "configs" / "demo.json"
    run = Pipeline(demo_root).run(load_manifest(manifest), load_config(config))
    ledger_path = demo_root / "data" / "runtime" / "snapshots" / "ledger.json"
    envelope = json.loads(ledger_path.read_text(encoding="utf-8"))
    envelope["payload"]["totals"]["effective_compute_units"] += 1
    ledger_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(BusinessError) as excinfo:
        Validator(demo_root, manifest, config).validate()
    assert excinfo.value.code == E_SNAPSHOT_CORRUPT
    assert excinfo.value.details["snapshot_category"] == "ledger"
    assert excinfo.value.details["snapshot_version"] == run["stage_version"]


def test_validator_reports_first_numeric_inconsistency(demo_root: Path) -> None:
    manifest = demo_root / "data" / "jobs.jsonl"
    config = demo_root / "configs" / "demo.json"
    run = Pipeline(demo_root).run(load_manifest(manifest), load_config(config))
    results_path = demo_root / "data" / "runtime" / "snapshots" / "results.json"
    envelope = json.loads(results_path.read_text(encoding="utf-8"))
    target = next(
        result
        for result in envelope["payload"]["results"]
        if result["job_id"] == "job-003"
    )
    target["summary"]["loss_first"] += 0.125
    envelope["checksum"] = envelope_checksum(
        "results",
        run["stage_version"],
        envelope["payload"],
        SNAPSHOT_SCHEMA_VERSION,
    )
    atomic_write_json(results_path, envelope)

    validation = Validator(demo_root, manifest, config).validate()
    assert validation["ok"] is False
    assert validation["business_code"] == E_INCONSISTENT
    mismatch = validation["first_inconsistency"]
    assert mismatch["job_id"] == "job-003"
    assert mismatch["field"] == "loss_first"
    assert mismatch["deviation"] == pytest.approx(0.125)


def test_validator_does_not_pass_ledger_business_mismatch(demo_root: Path) -> None:
    manifest = demo_root / "data" / "jobs.jsonl"
    config = demo_root / "configs" / "demo.json"
    run = Pipeline(demo_root).run(load_manifest(manifest), load_config(config))
    ledger_path = demo_root / "data" / "runtime" / "snapshots" / "ledger.json"
    envelope = json.loads(ledger_path.read_text(encoding="utf-8"))
    envelope["payload"]["entries"][0]["effective_compute_units"] += 1
    envelope["checksum"] = envelope_checksum(
        "ledger",
        run["stage_version"],
        envelope["payload"],
        SNAPSHOT_SCHEMA_VERSION,
    )
    atomic_write_json(ledger_path, envelope)
    results_path = demo_root / "data" / "runtime" / "snapshots" / "results.json"
    results_envelope = json.loads(results_path.read_text(encoding="utf-8"))
    results_envelope["payload"]["ledger_checksum"] = envelope["checksum"]
    results_envelope["checksum"] = envelope_checksum(
        "results",
        run["stage_version"],
        results_envelope["payload"],
        SNAPSHOT_SCHEMA_VERSION,
    )
    atomic_write_json(results_path, results_envelope)

    validation = Validator(demo_root, manifest, config).validate()
    assert validation["ok"] is False
    assert validation["business_code"] == E_INCONSISTENT
    assert validation["first_inconsistency"]["field"] in {
        "ledger_allocations",
        "ledger_budget_totals",
    }


def test_validator_detects_cross_snapshot_version_conflict(
    demo_root: Path,
) -> None:
    manifest = demo_root / "data" / "jobs.jsonl"
    config = demo_root / "configs" / "demo.json"
    run = Pipeline(demo_root).run(load_manifest(manifest), load_config(config))
    plan_path = demo_root / "data" / "runtime" / "snapshots" / "plan.json"
    envelope = json.loads(plan_path.read_text(encoding="utf-8"))
    new_version = run["stage_version"] + 1
    envelope["version"] = new_version
    envelope["payload"]["stage_version"] = new_version
    envelope["checksum"] = envelope_checksum(
        "plan",
        new_version,
        envelope["payload"],
        SNAPSHOT_SCHEMA_VERSION,
    )
    atomic_write_json(plan_path, envelope)

    validation = Validator(demo_root, manifest, config).validate()
    assert validation["ok"] is False
    assert validation["business_code"] == E_SNAPSHOT_VERSION_CONFLICT


def test_dashboard_state_exposes_plan_results_and_errors(demo_root: Path) -> None:
    manifest = demo_root / "data" / "jobs.jsonl"
    config = demo_root / "configs" / "demo.json"
    Pipeline(demo_root).run(load_manifest(manifest), load_config(config))
    state = build_state(demo_root, manifest, config)
    assert state["snapshot_error"] is None
    assert len(state["manifest"]) == 8
    assert state["plan"]["totals"]["admitted"] == 7
    assert state["ledger"]["state"] == "settled"
    codes = {error["business_code"] for error in state["business_errors"]}
    assert E_COMPUTE_BUDGET in codes
    assert E_INJECTED_FAULT in codes
