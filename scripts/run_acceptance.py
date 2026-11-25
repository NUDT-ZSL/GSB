#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from latent_diag.config import load_config
from latent_diag.executor import run_engine
from latent_diag.generator import generate_samples
from latent_diag.selfcheck import run_selfcheck


SAMPLES = ROOT / "samples"
ARTIFACTS = ROOT / "artifacts" / "acceptance"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "latent_diag", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def _run(
    *,
    state: Path,
    tasks: str = "tasks.jsonl",
    config: str = "config.json",
    worker_count: int = 2,
    tasks_per_batch: int = 2,
    recompute_committed: bool = False,
    crash_after_checkpoints: int | None = None,
) -> dict[str, Any]:
    return run_engine(
        tasks_path=SAMPLES / tasks,
        config_path=SAMPLES / config,
        db_path=state / "ledger.sqlite",
        state_dir=state,
        worker_count=worker_count,
        tasks_per_batch=tasks_per_batch,
        recompute_committed=recompute_committed,
        crash_after_checkpoints=crash_after_checkpoints,
    )


def _ledger_facts(state: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(state / "ledger.sqlite")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT task_id, content_hash, max_abs, max_rel, amplification, "
        "dominant_source, tolerance_ok, payload_json "
        "FROM ledger_entries ORDER BY task_id"
    ).fetchall()
    facts = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        metrics = payload["metrics"]
        facts.append(
            {
                "task_id": row["task_id"],
                "ledger_hash": row["content_hash"],
                "max_abs": row["max_abs"],
                "max_rel": row["max_rel"],
                "error_amplification": row["amplification"],
                "dominant_source": row["dominant_source"],
                "tolerance_ok": bool(row["tolerance_ok"]),
                "boundary_max_abs": metrics["boundary_max_abs"],
                "quantization_floor_error": metrics["quantization_floor_error"],
                "normalization_delta": metrics["normalization_delta"],
                "block_scheduling_excess": metrics["block_scheduling_excess"],
                "profile_consistent_scheduling_max_abs": metrics[
                    "profile_consistent_scheduling_max_abs"
                ],
                "block_schedule_hash": payload["block_schedule_hash"],
            }
        )
    con.close()
    return facts


def _ledger_hashes(state: Path) -> dict[str, str]:
    return {item["task_id"]: item["ledger_hash"] for item in _ledger_facts(state)}


def _selfcheck(state: Path, config: str = "config.json") -> dict[str, Any]:
    return run_selfcheck(
        db_path=state / "ledger.sqlite",
        state_dir=state,
        config=load_config(SAMPLES / config),
    )


def main() -> int:
    if ARTIFACTS.exists():
        shutil.rmtree(ARTIFACTS)
    ARTIFACTS.mkdir(parents=True)
    sample_metadata = generate_samples(SAMPLES, seed=20261126)
    report: dict[str, Any] = {
        "ok": True,
        "sample_generator": {
            "seed": 20261126,
            "task_count": sample_metadata["task_count"],
            "config_hash": sample_metadata["config_hash"],
            "tasks_hash": sample_metadata["tasks_hash"],
            "latent_hashes": sample_metadata["latent_hashes"],
        },
        "checks": [],
    }

    baseline = ARTIFACTS / "baseline"
    baseline_summary = _run(state=baseline)
    baseline_facts = _ledger_facts(baseline)
    baseline_selfcheck = _selfcheck(baseline)
    report["baseline"] = {
        "summary": baseline_summary,
        "ledger": baseline_facts,
        "selfcheck_ok": baseline_selfcheck["ok"],
    }
    float64_facts = [
        item
        for item in baseline_facts
        if item["task_id"] in {"t001_forest_f64", "t004_robot_f64"}
    ]
    report["checks"].append(
        {
            "name": "baseline_all_tasks_completed",
            "ok": baseline_summary["status"] == "ok"
            and len(baseline_summary["results"]["recomputed"]) == 6,
        }
    )
    report["checks"].append(
        {
            "name": "float64_blockwise_equals_full_reference",
            "ok": all(item["max_abs"] == 0.0 for item in float64_facts)
            and all(
                item["profile_consistent_scheduling_max_abs"] == 0.0
                for item in baseline_facts
            ),
        }
    )
    report["checks"].append(
        {"name": "baseline_selfcheck", "ok": baseline_selfcheck["ok"]}
    )

    reuse_summary = _run(state=baseline)
    report["repeat_reuse"] = reuse_summary
    report["checks"].append(
        {
            "name": "unchanged_tasks_reused",
            "ok": reuse_summary["results"]["recomputed"] == []
            and len(reuse_summary["results"]["reused"]) == 6
            and reuse_summary["results"]["reuse_recompute_disjoint"],
        }
    )

    changed_summary = _run(state=baseline, tasks="tasks_v2_dtype_change.jsonl")
    changed_selfcheck = _selfcheck(baseline)
    report["revision_invalidation"] = {
        "summary": changed_summary,
        "selfcheck_ok": changed_selfcheck["ok"],
    }
    report["checks"].append(
        {
            "name": "revision_invalidation_and_downstream_propagation",
            "ok": changed_summary["invalidation"]["affected"]
            == ["t002_city_f32", "t004_robot_f64"]
            and changed_summary["results"]["recomputed"]
            == ["t002_city_f32", "t004_robot_f64"]
            and set(changed_summary["results"]["reused"])
            == {
                "t001_forest_f64",
                "t003_ocean_fixed",
                "t005_desert_f32",
                "t006_snow_fixed",
            }
            and changed_summary["results"]["reuse_recompute_disjoint"]
            and changed_selfcheck["ok"],
        }
    )

    iter_state = ARTIFACTS / "budget_iter"
    iter_summary = _run(state=iter_state, config="config_budget_iter.json")
    iter_selfcheck = _selfcheck(iter_state, "config_budget_iter.json")
    report["iteration_budget"] = {
        "summary": iter_summary,
        "selfcheck_ok": iter_selfcheck["ok"],
    }
    report["checks"].append(
        {
            "name": "iteration_budget_rejection",
            "ok": "E_ITER_BUDGET" in iter_summary["error_codes"]
            and iter_summary["admission"]["rejected"]
            and iter_summary["budget_usage"]["total_denoise_iterations"]
            <= iter_summary["budget_usage"]["max_total_denoise_iterations"]
            and iter_selfcheck["ok"],
        }
    )

    memory_state = ARTIFACTS / "budget_memory"
    memory_summary = _run(state=memory_state, config="config_budget_memory.json")
    memory_selfcheck = _selfcheck(memory_state, "config_budget_memory.json")
    report["memory_budget"] = {
        "summary": memory_summary,
        "selfcheck_ok": memory_selfcheck["ok"],
    }
    report["checks"].append(
        {
            "name": "memory_budget_rejection",
            "ok": "E_MEMORY_BUDGET" in memory_summary["error_codes"]
            and memory_summary["admission"]["rejected"]
            and memory_selfcheck["ok"],
        }
    )

    tolerance_state = ARTIFACTS / "tolerance"
    tolerance_summary = _run(
        state=tolerance_state,
        config="config_tolerance_tight.json",
    )
    tolerance_selfcheck = _selfcheck(tolerance_state, "config_tolerance_tight.json")
    failures = tolerance_summary["results"]["tolerance_failures"]
    report["tolerance"] = {
        "summary": tolerance_summary,
        "selfcheck_ok": tolerance_selfcheck["ok"],
    }
    report["checks"].append(
        {
            "name": "tolerance_error_location",
            "ok": bool(failures)
            and all(
                item["code"] == "E_TOLERANCE_EXCEEDED"
                and item["task_id"]
                and len(item["first_deviation"]) == 3
                for item in failures
            )
            and tolerance_selfcheck["ok"],
        }
    )

    crash_state = ARTIFACTS / "crash_recovery"
    crashed = _cli(
        "run",
        "--tasks",
        str(SAMPLES / "tasks.jsonl"),
        "--config",
        str(SAMPLES / "config.json"),
        "--db",
        str(crash_state / "ledger.sqlite"),
        "--state",
        str(crash_state),
        "--workers",
        "2",
        "--batch-size",
        "2",
        "--crash-after-checkpoints",
        "2",
    )
    con = sqlite3.connect(crash_state / "ledger.sqlite")
    checkpoint_count = con.execute(
        "SELECT COUNT(*) FROM checkpoint_entries"
    ).fetchone()[0]
    con.close()
    resumed = _run(state=crash_state)
    hashes_after_resume = _ledger_hashes(crash_state)
    resumed_selfcheck = _selfcheck(crash_state)
    recomputed = _run(state=crash_state, recompute_committed=True)
    recomputed_selfcheck = _selfcheck(crash_state)
    hashes_after_recompute = _ledger_hashes(crash_state)
    report["forced_recovery"] = {
        "crash_returncode": crashed.returncode,
        "checkpoints_before_restart": checkpoint_count,
        "resume_summary": resumed,
        "recompute_committed_summary": recomputed,
        "selfcheck_after_resume": resumed_selfcheck["ok"],
        "selfcheck_after_recompute": recomputed_selfcheck["ok"],
        "ledger_hashes_stable": hashes_after_resume == hashes_after_recompute,
    }
    report["checks"].append(
        {
            "name": "forced_recovery_ledger_consistency",
            "ok": crashed.returncode in (-signal.SIGKILL, 137)
            and checkpoint_count == 2
            and resumed["results"]["reused"]
            == ["t001_forest_f64", "t002_city_f32"]
            and len(resumed["results"]["recomputed"]) == 4
            and resumed["results"]["reuse_recompute_disjoint"]
            and resumed_selfcheck["ok"]
            and recomputed_selfcheck["ok"]
            and hashes_after_resume == hashes_after_recompute,
        }
    )

    pytest_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    report["pytest"] = {
        "returncode": pytest_result.returncode,
        "stdout": pytest_result.stdout.strip(),
        "stderr": pytest_result.stderr.strip(),
    }
    report["checks"].append(
        {"name": "automated_test_suite", "ok": pytest_result.returncode == 0}
    )
    report["ok"] = all(item["ok"] for item in report["checks"])

    report_path = ROOT / "artifacts" / "validation_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "report": str(report_path),
                "checks": report["checks"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

