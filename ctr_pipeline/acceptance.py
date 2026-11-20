from __future__ import annotations

import json
import multiprocessing
import platform
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .canonical import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    checksum,
)
from .errors import PipelineError
from .generator import generate_dataset
from .ingestion import (
    canonical_expanded_bytes,
    expand_dataset,
    load_feature_dictionary,
    load_source_samples,
)
from .models import BudgetConfig, TrainingConfig
from .pipeline import run_pipeline
from .snapshots import (
    BudgetLedger,
    SnapshotCache,
    SnapshotStore,
    SubmissionGate,
    submit_schedule_plan,
)


def _jsonl_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_write_bytes(
        path,
        b"".join(canonical_json_bytes(row) + b"\n" for row in rows),
    )


def _capture_error(callable_: Callable[[], Any]) -> dict[str, Any] | None:
    try:
        callable_()
    except PipelineError as exc:
        return exc.to_dict()
    return None


def _same_file(left: Path, right: Path) -> bool:
    return left.read_bytes() == right.read_bytes()


def run_acceptance(output_dir: str | Path) -> dict[str, Any]:
    output = Path(output_dir)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    generated_dir = output / "generated"
    generated_a = generated_dir / "a"
    generated_b = generated_dir / "b"
    checks: list[dict[str, Any]] = []

    def record(name: str, passed: bool, evidence: Any) -> None:
        checks.append(
            {
                "evidence": evidence,
                "name": name,
                "passed": bool(passed),
            }
        )

    manifest_a = generate_dataset(generated_a, seed=20261121, sample_count=60)
    manifest_b = generate_dataset(generated_b, seed=20261121, sample_count=60)
    generation_equal = (
        manifest_a == manifest_b
        and _same_file(generated_a / "exposures.jsonl", generated_b / "exposures.jsonl")
        and _same_file(generated_a / "features.jsonl", generated_b / "features.jsonl")
    )
    record(
        "deterministic_generation",
        generation_equal,
        {
            "dictionary_checksum": manifest_a["dictionary_checksum"],
            "exposures_checksum": manifest_a["exposures_checksum"],
            "seed": manifest_a["seed"],
            "sample_count": manifest_a["sample_count"],
        },
    )

    rows = _jsonl_rows(generated_a / "features.jsonl")
    shuffled_features = output / "features_shuffled.jsonl"
    _write_jsonl(shuffled_features, list(reversed(rows)))
    dictionary_a = load_feature_dictionary(
        generated_a / "features.jsonl", version="dict-v1"
    )
    dictionary_b = load_feature_dictionary(shuffled_features, version="dict-v1")
    sources_a = load_source_samples(
        generated_a / "exposures.jsonl", dictionary_a
    )
    sources_b = load_source_samples(
        generated_a / "exposures.jsonl", dictionary_b
    )
    expanded_a = expand_dataset(sources_a, dictionary_a, feature_dimension=128)
    expanded_b = expand_dataset(sources_b, dictionary_b, feature_dimension=128)
    expansion_equal = (
        canonical_expanded_bytes(expanded_a.samples)
        == canonical_expanded_bytes(expanded_b.samples)
    )
    record(
        "dictionary_order_independent_expansion",
        expansion_equal,
        {
            "dictionary_checksum": dictionary_a.checksum,
            "expanded_checksum": expanded_a.checksum,
            "sample_count": len(expanded_a.samples),
        },
    )

    common_training = {
        "batch_sample_size": 24,
        "chunk_size": 8,
        "learning_rate": 0.35,
        "max_rounds": 8,
        "tolerance": 1e-9,
    }
    budget = BudgetConfig(
        total_mac_budget=50_000_000,
        memory_budget_bytes=32 * 1024 * 1024,
    )
    result_w1 = run_pipeline(
        generated_a / "exposures.jsonl",
        generated_a / "features.jsonl",
        feature_dimension=128,
        training_config=TrainingConfig(workers=1, **common_training),
        budget_config=budget,
        output_dir=output / "run_w1",
    )
    result_w3 = run_pipeline(
        generated_a / "exposures.jsonl",
        generated_a / "features.jsonl",
        feature_dimension=128,
        training_config=TrainingConfig(workers=3, **common_training),
        budget_config=budget,
        output_dir=output / "run_w3",
    )
    deterministic_training = (
        result_w1["status"] == "ok"
        and result_w3["status"] == "ok"
        and result_w1["determinism_fingerprint"]
        == result_w3["determinism_fingerprint"]
        and checksum(result_w1["chunked"]["rounds"])
        == checksum(result_w3["chunked"]["rounds"])
        and result_w1["chunked"]["parameters"]
        == result_w3["chunked"]["parameters"]
    )
    record(
        "parallel_determinism",
        deterministic_training,
        {
            "fingerprint": result_w1["determinism_fingerprint"],
            "rounds_checksum": checksum(result_w1["chunked"]["rounds"]),
            "workers_compared": [1, 3],
        },
    )
    comparison = result_w1["comparison"]
    consistency_ok = (
        result_w1["validation"]["status"] == "ok"
        and comparison["max_abs_parameter_deviation"] < 1e-12
        and comparison["converged_round_equal"]
        and len(comparison["round_deltas"]) > 0
    )
    record(
        "chunk_vs_full_consistency",
        consistency_ok,
        {
            "auc": result_w1["summary"]["metrics"]["auc"],
            "chunked_converged_round": comparison["chunked_converged_round"],
            "converged_round_equal": comparison["converged_round_equal"],
            "full_converged_round": comparison["full_converged_round"],
            "log_loss": result_w1["summary"]["metrics"]["log_loss"],
            "max_abs_parameter_deviation": comparison[
                "max_abs_parameter_deviation"
            ],
            "parameter_deviation_spectrum_length": len(
                comparison["final_parameter_abs_deviation_spectrum"]
            ),
        },
    )

    compute_result = run_pipeline(
        generated_a / "exposures.jsonl",
        generated_a / "features.jsonl",
        feature_dimension=256,
        training_config=TrainingConfig(
            chunk_size=8,
            batch_sample_size=24,
            workers=4,
            max_rounds=8,
            tolerance=1e-9,
        ),
        budget_config=BudgetConfig(
            total_mac_budget=50_000,
            memory_budget_bytes=32 * 1024 * 1024,
        ),
    )
    compute_decision = compute_result["budget"]["decision"]
    compute_budget_ok = (
        compute_result["summary"]["budget_error_code"] == "E_COMPUTE_BUDGET"
        and compute_decision["degraded"]
        and compute_decision["rejected_sample_count"] > 0
        and compute_result["validation"]["status"] == "ok"
    )
    record(
        "compute_budget_degradation",
        compute_budget_ok,
        {
            "admitted_samples": compute_decision["admitted_sample_count"],
            "degrade_categories": compute_decision["degrade_categories"],
            "effective_chunk_size": compute_decision["effective_chunk_size"],
            "effective_workers": compute_decision["effective_workers"],
            "estimated_macs": compute_decision["estimated_macs"],
            "rejected_samples": compute_decision["rejected_sample_count"],
        },
    )

    memory_result = run_pipeline(
        generated_a / "exposures.jsonl",
        generated_a / "features.jsonl",
        feature_dimension=256,
        training_config=TrainingConfig(
            chunk_size=8,
            batch_sample_size=24,
            workers=3,
            max_rounds=8,
            tolerance=1e-9,
        ),
        budget_config=BudgetConfig(
            total_mac_budget=50_000_000,
            memory_budget_bytes=80_000,
        ),
    )
    memory_decision = memory_result["budget"]["decision"]
    memory_budget_ok = (
        memory_result["summary"]["budget_error_code"] == "E_MEMORY_LIMIT"
        and memory_decision["degraded"]
        and memory_decision["rejected_sample_count"] > 0
        and memory_result["validation"]["status"] == "ok"
    )
    record(
        "memory_budget_degradation",
        memory_budget_ok,
        {
            "admitted_samples": memory_decision["admitted_sample_count"],
            "degrade_categories": memory_decision["degrade_categories"],
            "effective_chunk_size": memory_decision["effective_chunk_size"],
            "effective_workers": memory_decision["effective_workers"],
            "estimated_memory_bytes": memory_decision["estimated_memory_bytes"],
            "rejected_samples": memory_decision["rejected_sample_count"],
        },
    )

    fault_result = run_pipeline(
        generated_a / "exposures.jsonl",
        generated_a / "features.jsonl",
        feature_dimension=128,
        training_config=TrainingConfig(
            chunk_size=8,
            batch_sample_size=24,
            workers=2,
            max_rounds=3,
            tolerance=1e-9,
            fault_chunk_ids=(1,),
        ),
        budget_config=budget,
    )
    failed_errors = fault_result["chunked"]["errors"]
    fault_ok = (
        fault_result["status"] == "partial"
        and len(fault_result["chunked"]["partial_chunks"]) == 8
        and len(failed_errors) == 1
        and failed_errors[0]["code"] == "E_WORKER"
        and failed_errors[0]["stage"] == "chunk_gradient"
        and failed_errors[0]["details"]["batch_index"] == 0
        and failed_errors[0]["details"]["chunk_index"] == 1
    )
    record(
        "worker_failure_attribution",
        fault_ok,
        {
            "error": failed_errors[0] if failed_errors else None,
            "status": fault_result["status"],
            "successful_other_chunks": sum(
                1
                for outcome in fault_result["chunked"]["partial_chunks"]
                if outcome["ok"]
            ),
        },
    )

    duplicate_path = output / "exposures_duplicate.jsonl"
    rows = _jsonl_rows(generated_a / "exposures.jsonl")
    _write_jsonl(duplicate_path, rows + [rows[0]])
    label_path = output / "exposures_bad_label.jsonl"
    bad_label_rows = [dict(row) for row in rows]
    bad_label_rows[0]["label"] = 2
    _write_jsonl(label_path, bad_label_rows)
    bad_dimension_path = output / "features_bad_dimension.jsonl"
    feature_rows = _jsonl_rows(generated_a / "features.jsonl")
    feature_rows[0]["dimension"] = 0
    _write_jsonl(bad_dimension_path, feature_rows)

    invalid_chunk = _capture_error(
        lambda: run_pipeline(
            generated_a / "exposures.jsonl",
            generated_a / "features.jsonl",
            feature_dimension=128,
            training_config=TrainingConfig(chunk_size=0),
        )
    )
    invalid_workers = _capture_error(
        lambda: run_pipeline(
            generated_a / "exposures.jsonl",
            generated_a / "features.jsonl",
            feature_dimension=128,
            training_config=TrainingConfig(workers=0),
        )
    )
    invalid_dimension = _capture_error(
        lambda: run_pipeline(
            generated_a / "exposures.jsonl",
            bad_dimension_path,
            feature_dimension=128,
        )
    )
    duplicate = _capture_error(
        lambda: run_pipeline(
            duplicate_path,
            generated_a / "features.jsonl",
            feature_dimension=128,
        )
    )
    bad_label = _capture_error(
        lambda: run_pipeline(
            label_path,
            generated_a / "features.jsonl",
            feature_dimension=128,
        )
    )
    validation_cases = {
        "bad_label": (bad_label, "E_CONFIG_INVALID", "label"),
        "bad_dimension": (invalid_dimension, "E_CONFIG_INVALID", "dimension"),
        "chunk_size": (invalid_chunk, "E_CONFIG_INVALID", "chunk_size"),
        "duplicate_sample": (duplicate, "E_DUP_SAMPLE", "sample_id"),
        "workers": (invalid_workers, "E_CONFIG_INVALID", "workers"),
    }
    validation_ok = all(
        error is not None
        and error["code"] == expected_code
        and error["details"].get("field") == expected_field
        for error, expected_code, expected_field in validation_cases.values()
    )
    record(
        "input_validation_error_codes",
        validation_ok,
        {
            name: {
                "code": error["code"] if error else None,
                "field": error["details"].get("field") if error else None,
                "stage": error["stage"] if error else None,
            }
            for name, (error, _, _) in validation_cases.items()
        },
    )

    snapshot_dir = output / "pipeline_snapshots"
    snapshot_result_v1 = run_pipeline(
        generated_a / "exposures.jsonl",
        generated_a / "features.jsonl",
        feature_dimension=128,
        training_config=TrainingConfig(
            chunk_size=8,
            batch_sample_size=24,
            workers=2,
            max_rounds=8,
            tolerance=1e-9,
        ),
        budget_config=budget,
        snapshot_dir=snapshot_dir,
        snapshot_version=1,
        submission_lock_path=output / "submission.lock",
    )
    snapshot_result_v2 = run_pipeline(
        generated_a / "exposures.jsonl",
        generated_a / "features.jsonl",
        feature_dimension=128,
        training_config=TrainingConfig(
            chunk_size=8,
            batch_sample_size=24,
            workers=2,
            max_rounds=8,
            tolerance=1e-9,
        ),
        budget_config=BudgetConfig(
            total_mac_budget=50_000,
            memory_budget_bytes=32 * 1024 * 1024,
        ),
        snapshot_dir=snapshot_dir,
        snapshot_version=2,
        submission_lock_path=output / "submission.lock",
    )
    snapshot_store = SnapshotStore(snapshot_dir)
    loaded_snapshots = snapshot_store.load_all()
    snapshots_ok = (
        set(loaded_snapshots) == {"schedule_plan", "budget_ledger", "training_summary"}
        and all(
            snapshot["version"] == 2 for snapshot in loaded_snapshots.values()
        )
        and loaded_snapshots["schedule_plan"]["payload"]["budget_decision"][
            "admitted_sample_count"
        ]
        == snapshot_result_v2["summary"]["admitted_sample_count"]
        and loaded_snapshots["training_summary"]["payload"]["sample_count"]
        == snapshot_result_v2["summary"]["sample_count"]
    )
    record(
        "versioned_whole_snapshot_replacement",
        snapshots_ok,
        {
            "objects": {
                name: {
                    "checksum": snapshot["checksum"],
                    "version": snapshot["version"],
                }
                for name, snapshot in sorted(loaded_snapshots.items())
            },
            "v1_admitted_samples": snapshot_result_v1["summary"][
                "admitted_sample_count"
            ],
            "v2_admitted_samples": snapshot_result_v2["summary"][
                "admitted_sample_count"
            ],
        },
    )

    corrupt_dir = output / "corrupt_snapshot"
    corrupt_store = SnapshotStore(corrupt_dir)
    corrupt_store.publish(
        "training_summary",
        {"sample_count": 1},
        version=7,
    )
    corrupt_path = corrupt_dir / "training_summary.json"
    corrupt_envelope = json.loads(corrupt_path.read_text(encoding="utf-8"))
    corrupt_envelope["checksum"] = "0" * 64
    atomic_write_json(corrupt_path, corrupt_envelope)
    corrupt_error = _capture_error(lambda: corrupt_store.load("training_summary"))
    corrupt_ok = (
        corrupt_error is not None
        and corrupt_error["code"] == "E_SNAPSHOT_CORRUPT"
        and corrupt_error["details"]["object_name"] == "training_summary"
        and corrupt_error["details"]["version"] == 7
    )
    record(
        "snapshot_corruption_detection",
        corrupt_ok,
        {"error": corrupt_error},
    )

    cache = SnapshotCache()
    v1_envelope = {
        "checksum": "a" * 64,
        "object_name": "schedule_plan",
        "payload": {"mode": "full", "nested": {"kept_only_in_v1": True}},
        "schema_version": 1,
        "version": 1,
    }
    v2_envelope = {
        "checksum": "b" * 64,
        "object_name": "schedule_plan",
        "payload": {"mode": "restricted"},
        "schema_version": 1,
        "version": 2,
    }
    cache.reconcile(v1_envelope)
    cache.reconcile(v2_envelope)
    cache_ok = (
        cache.objects["schedule_plan"]["payload"] == {"mode": "restricted"}
        and len(cache.replacements) == 2
    )
    record(
        "restart_cache_whole_object_reconcile",
        cache_ok,
        {"replacements": cache.replacements},
    )

    gate_lock = output / "gate.lock"
    busy_error: dict[str, Any] | None = None
    gate_holder = SubmissionGate(gate_lock)
    gate_holder.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: submit_schedule_plan(
                    SubmissionGate(gate_lock),
                    SnapshotStore(output / "gate_snapshots"),
                    {"mode": "second"},
                    version=1,
                )
            )
            busy_error = future.result()
    except PipelineError as exc:
        busy_error = exc.to_dict()
    finally:
        gate_holder.release()
    busy_ok = (
        busy_error is not None and busy_error["code"] == "E_STAGE_BUSY"
    )
    record(
        "concurrent_submission_rejected_immediately",
        busy_ok,
        {"error": busy_error},
    )

    ledger = BudgetLedger(1000)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: ledger.debit(f"tx-{index:03d}", 1), range(100)))
    for index in range(10):
        ledger.release(f"tx-{index:03d}")
    ledger_snapshot = ledger.snapshot_payload()
    ledger_ok = (
        ledger_snapshot["balance"] == 910
        and len(ledger_snapshot["entries"]) == 90
        and ledger_snapshot["sequence"] == 110
    )
    record(
        "atomic_ledger_debit_release",
        ledger_ok,
        {
            "balance": ledger_snapshot["balance"],
            "reserved_entries": len(ledger_snapshot["entries"]),
            "sequence": ledger_snapshot["sequence"],
        },
    )

    report = {
        "all_passed": all(check["passed"] for check in checks),
        "checks": checks,
        "environment": {
            "multiprocessing_start_method": multiprocessing.get_start_method(),
            "numpy_version": np.__version__,
            "platform": platform.platform(),
            "python_version": sys.version.split()[0],
        },
        "generated": manifest_a,
    }
    atomic_write_json(output / "acceptance_report.json", report)
    return report
