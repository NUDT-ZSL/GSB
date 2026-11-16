"""End-to-end acceptance checks using isolated local state directories."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json
from .config import load_experiment
from .data import build_dataset
from .fingerprint import config_hash
from .lab import ExperimentSession

VERIFICATION_SCHEMA_VERSION = 1


def _trajectory_difference(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    first_trajectory = first.get("loss_trajectory", [])
    second_trajectory = second.get("loss_trajectory", [])
    length_equal = len(first_trajectory) == len(second_trajectory)
    max_loss_difference = 0.0
    if length_equal:
        for left, right in zip(first_trajectory, second_trajectory):
            for key in ("data_loss", "regularization_loss", "total_loss", "update_norm"):
                max_loss_difference = max(
                    max_loss_difference,
                    abs(float(left[key]) - float(right[key])),
                )
    left_final = first.get("final_coefficients", {})
    right_final = second.get("final_coefficients", {})
    left_weights = left_final.get("weights", [])
    right_weights = right_final.get("weights", [])
    shape_equal = len(left_weights) == len(right_weights)
    max_weight_difference = 0.0
    if shape_equal:
        for left, right in zip(left_weights, right_weights):
            max_weight_difference = max(max_weight_difference, abs(float(left) - float(right)))
        max_weight_difference = max(
            max_weight_difference,
            abs(float(left_final.get("intercept", 0.0)) - float(right_final.get("intercept", 0.0))),
        )
    return {
        "trajectory_length_equal": length_equal,
        "coefficient_shape_equal": shape_equal,
        "max_abs_loss_difference": max_loss_difference,
        "max_abs_coefficient_difference": max_weight_difference,
        "exact": bool(
            length_equal
            and shape_equal
            and max_loss_difference == 0.0
            and max_weight_difference == 0.0
        ),
    }


def _summarize_scheme(result: dict[str, Any]) -> dict[str, Any]:
    trajectory = result.get("loss_trajectory", [])
    last = trajectory[-1] if trajectory else {}
    return {
        "status": result.get("status"),
        "converged": result.get("converged"),
        "iterations_completed": result.get("iterations_completed"),
        "initial_total_loss": trajectory[0]["total_loss"] if trajectory else None,
        "final_total_loss": last.get("total_loss"),
        "final_data_loss": last.get("data_loss"),
        "final_regularization_loss": last.get("regularization_loss"),
        "final_regularization_components": last.get("regularization_components"),
        "validation_mse": result.get("validation_mse"),
        "snapshot_version": result.get("snapshot_version"),
    }


def run_verification(config_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    base_config = load_experiment(config_path)
    first_dataset, first_data_failures = build_dataset(base_config)
    second_dataset, second_data_failures = build_dataset(base_config)
    data_deterministic = bool(
        first_dataset is not None
        and second_dataset is not None
        and first_dataset.data_fingerprint == second_dataset.data_fingerprint
        and not first_data_failures
        and not second_data_failures
    )
    data_check = {
        "passed": data_deterministic,
        "data_fingerprint": first_dataset.data_fingerprint if first_dataset is not None else None,
        "train_size": first_dataset.train_size if first_dataset is not None else None,
        "validation_size": first_dataset.validation_size if first_dataset is not None else None,
        "feature_count": first_dataset.feature_count if first_dataset is not None else None,
        "failure_count": len(first_data_failures) + len(second_data_failures),
    }

    checks: dict[str, Any] = {"data_determinism": data_check}
    if first_dataset is None or base_config.regularizers == []:
        report = {
            "verification_schema_version": VERIFICATION_SCHEMA_VERSION,
            "checks": checks,
            "all_checks_passed": False,
        }
        atomic_write_json(output_path, report, indent=2)
        return report

    with tempfile.TemporaryDirectory(prefix="regularization-lab-verify-") as temporary:
        root = Path(temporary)
        serial_config = replace(
            base_config,
            runtime=replace(
                base_config.runtime,
                concurrency=1,
                state_dir=root / "serial-state",
                report_path=root / "serial-report.json",
                round_delay_seconds=0.0,
            ),
        )
        concurrent_config = replace(
            base_config,
            runtime=replace(
                base_config.runtime,
                concurrency=4,
                state_dir=root / "concurrent-state",
                report_path=root / "concurrent-report.json",
                round_delay_seconds=0.0,
            ),
        )

        serial_session = ExperimentSession(serial_config)
        serial_results = serial_session.run_all()
        serial_report = serial_session.write_report()
        concurrent_session = ExperimentSession(concurrent_config)
        concurrent_results = concurrent_session.run_all()
        concurrent_report = concurrent_session.write_report()

        serial_by_name = {result["scheme"]: result for result in serial_results}
        concurrent_by_name = {result["scheme"]: result for result in concurrent_results}
        training_details: dict[str, Any] = {}
        training_passed = True
        for name, result in serial_by_name.items():
            summary = _summarize_scheme(result)
            summary["regularization"] = result.get("regularization")
            training_details[name] = summary
            if result.get("status") != "complete" or not result.get("converged", False):
                training_passed = False
        kinds = {result.get("regularization", {}).get("type") for result in serial_results}
        training_passed = training_passed and kinds == {"l2", "l1", "elastic_net"}
        checks["three_regularizers"] = {
            "passed": training_passed,
            "schemes": training_details,
        }

        max_loss_difference = 0.0
        max_coefficient_difference = 0.0
        per_scheme: dict[str, Any] = {}
        for name in serial_by_name:
            difference = _trajectory_difference(serial_by_name[name], concurrent_by_name[name])
            per_scheme[name] = difference
            max_loss_difference = max(max_loss_difference, difference["max_abs_loss_difference"])
            max_coefficient_difference = max(
                max_coefficient_difference,
                difference["max_abs_coefficient_difference"],
            )
        reports_identical = serial_report == concurrent_report
        concurrency_passed = bool(
            reports_identical
            and max_loss_difference <= base_config.runtime.numeric_tolerance
            and max_coefficient_difference <= base_config.runtime.numeric_tolerance
        )
        checks["serial_vs_concurrent"] = {
            "passed": concurrency_passed,
            "serial_concurrency": 1,
            "concurrent_concurrency": 4,
            "reports_identical": reports_identical,
            "max_abs_loss_difference": max_loss_difference,
            "max_abs_coefficient_difference": max_coefficient_difference,
            "declared_tolerance": base_config.runtime.numeric_tolerance,
            "business_codes": [] if concurrency_passed else ["E_NUMERIC_DRIFT"],
            "per_scheme": per_scheme,
        }

        replay_session = ExperimentSession(
            replace(
                concurrent_config,
                runtime=replace(
                    concurrent_config.runtime,
                    report_path=root / "replay-report.json",
                ),
            )
        )
        replay_results = replay_session.run_all()
        replay_details = {
            result["scheme"]: {
                "cache_hit": result.get("cache_decision", {}).get("cache_hit"),
                "training_executed": result.get("cache_decision", {}).get("training_executed"),
                "reused_rounds": len(result.get("cache_decision", {}).get("reused_rounds", [])),
                "recomputed_rounds": result.get("cache_decision", {}).get("recomputed_rounds"),
            }
            for result in replay_results
        }
        replay_passed = all(
            details["cache_hit"]
            and not details["training_executed"]
            and details["recomputed_rounds"] == []
            for details in replay_details.values()
        ) and all(
            replay_details[name]["cache_hit"] for name in serial_by_name
        )
        checks["cache_replay"] = {
            "passed": replay_passed,
            "schemes": replay_details,
        }

        baseline_ridge = concurrent_by_name[base_config.regularizers[0].name]
        recovery_state = root / "recovery-state"
        paused_session = ExperimentSession(
            replace(
                concurrent_config,
                runtime=replace(
                    concurrent_config.runtime,
                    state_dir=recovery_state,
                    report_path=root / "paused-report.json",
                ),
            )
        )
        pause_round = 40
        paused_result = paused_session.run_scheme(
            base_config.regularizers[0].name,
            pause_after_round=pause_round,
        )
        resumed_session = ExperimentSession(
            replace(
                concurrent_config,
                runtime=replace(
                    concurrent_config.runtime,
                    state_dir=recovery_state,
                    report_path=root / "resumed-report.json",
                ),
            )
        )
        resumed_result = resumed_session.run_scheme(base_config.regularizers[0].name)
        recovery_difference = _trajectory_difference(baseline_ridge, resumed_result)
        recovery_passed = bool(
            paused_result is not None
            and paused_result.get("status") == "interrupted"
            and resumed_result is not None
            and resumed_result.get("resumed_from_round") == pause_round
            and recovery_difference["exact"]
        )
        checks["snapshot_recovery"] = {
            "passed": recovery_passed,
            "scheme": base_config.regularizers[0].name,
            "paused_at_round": pause_round,
            "paused_snapshot_version": paused_result.get("snapshot_version") if paused_result else None,
            "resumed_from_round": resumed_result.get("resumed_from_round") if resumed_result else None,
            "comparison_to_uninterrupted": recovery_difference,
            "business_codes": [] if recovery_difference["exact"] else ["E_NUMERIC_DRIFT"],
        }

        mismatch_state = root / "mismatch-state"
        mismatch_session = ExperimentSession(
            replace(
                concurrent_config,
                runtime=replace(
                    concurrent_config.runtime,
                    state_dir=mismatch_state,
                    report_path=root / "mismatch-paused-report.json",
                ),
            )
        )
        mismatch_session.run_scheme(
            base_config.regularizers[0].name,
            pause_after_round=20,
        )
        changed_regularizers = list(base_config.regularizers)
        changed_regularizers[0] = replace(
            changed_regularizers[0],
            alpha=changed_regularizers[0].alpha + 0.01,
        )
        changed_config = replace(
            concurrent_config,
            regularizers=changed_regularizers,
            runtime=replace(
                concurrent_config.runtime,
                state_dir=mismatch_state,
                report_path=root / "mismatch-report.json",
            ),
        )
        changed_session = ExperimentSession(changed_config)
        changed_hash = config_hash(
            changed_config,
            changed_session.dataset,
            changed_session.schemes[base_config.regularizers[0].name],
        ) if changed_session.dataset is not None else ""
        resume_payload = None
        mismatch_codes: list[str] = []
        if changed_session.dataset is not None:
            resume_payload, load_failures = changed_session.snapshot_store.load_latest(
                config_hash=changed_hash,
                config_schema_version=changed_config.schema_version,
                known_config_hashes=changed_session.known_config_hashes(),
            )
            mismatch_codes = sorted(
                {
                    item["business_code"]
                    for item in changed_session.failures
                    if item.get("business_code") == "E_CONFIG_MISMATCH"
                }
                | {failure.business_code for failure in load_failures}
            )
        checks["config_change_invalidation"] = {
            "passed": bool(resume_payload is None and "E_CONFIG_MISMATCH" in mismatch_codes),
            "changed_scheme": base_config.regularizers[0].name,
            "old_alpha": base_config.regularizers[0].alpha,
            "new_alpha": changed_regularizers[0].alpha,
            "resume_payload_found": resume_payload is not None,
            "business_codes": mismatch_codes,
        }

    all_checks_passed = all(check.get("passed", False) for check in checks.values())
    report = {
        "verification_schema_version": VERIFICATION_SCHEMA_VERSION,
        "checks": checks,
        "all_checks_passed": all_checks_passed,
    }
    atomic_write_json(output_path, report, indent=2)
    return report
