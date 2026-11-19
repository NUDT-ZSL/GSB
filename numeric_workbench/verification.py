"""Compact, auditable verification summaries derived from a full report."""

from __future__ import annotations

from typing import Any


def build_verification_summary(report: dict[str, Any]) -> dict[str, Any]:
    execution = report["execution"]
    spectrum = report.get("error_spectrum") or {}
    verification = spectrum.get("verification", {}) if spectrum else {}
    snapshot = report.get("snapshot") or {}
    precision_switch = report.get("precision_switch") or {}
    variants = precision_switch.get("variants", {})
    chunk_full = report.get("chunk_full_consistency") or {}
    return {
        "schema": "numeric-workbench/verification-summary/v1",
        "job_id": report["job_id"],
        "status": report["status"],
        "input_sha256": report["input"]["sha256"],
        "config_sha256": report["config_sha256"],
        "row_count": report["input"]["row_count"],
        "train_count": report["input"]["train_count"],
        "validation_count": report["input"]["validation_count"],
        "primary_run": {
            "precision": execution["precision"],
            "reduce_mode": execution["reduce_mode"],
            "rounds": execution["primary_run"]["rounds"],
            "stop_reason": execution["primary_run"]["stop_reason"],
            "core_sha256": execution["primary_run"]["core_sha256"],
            "trajectory_sha256": execution["primary_run"]["trajectory_sha256"],
        },
        "chunk_full_consistency": chunk_full,
        "precision_switch_contract": precision_switch.get("contract"),
        "precisions": (
            {
                precision: {
                    "chunked_rounds": variants[precision]["chunked"]["rounds"],
                    "chunked_stop_reason": variants[precision]["chunked"]["stop_reason"],
                    "chunked_final_loss": variants[precision]["chunked"][
                        "loss_trajectory"
                    ][-1],
                    "core_sha256": variants[precision]["chunked"]["core_sha256"],
                    "vs_float64_final_parameter_max_abs": variants[precision][
                        "vs_reference_full"
                    ]["absolute"]["final_parameter"]["max"],
                    "fixed_replay": variants[precision].get("fixed_replay"),
                }
                for precision in ("float64", "float32", "fixed_point")
            }
            if variants
            else {}
        ),
        "dominant_source": (
            {
                "precision": spectrum["dominant_source"]["precision"],
                "stage": spectrum["dominant_source"]["stage"],
                "severity": spectrum["dominant_source"]["severity"],
                "amplification_ratio": spectrum["dominant_source"][
                    "amplification_ratio"
                ],
                "worst_detail": spectrum["dominant_source"]["worst_detail"],
                "detail_location": spectrum["dominant_source"]["detail_location"],
                "detail_count": spectrum["dominant_source"]["detail_count"],
            }
            if spectrum
            else None
        ),
        "recompute": {
            "trajectory_match": verification.get("trajectory_recompute_match"),
            "error_spectrum_match": verification.get("error_spectrum_recompute_match"),
            "first_spectrum_sha256": verification.get("first_spectrum_sha256"),
            "recomputed_spectrum_sha256": verification.get(
                "recomputed_spectrum_sha256"
            ),
            "repeatability_by_precision": verification.get("repeatability_by_precision"),
        },
        "workbench_checks": report.get("workbench_checks"),
        "checkpoint": execution.get("checkpoint"),
        "snapshot": {
            "path": snapshot.get("path"),
            "snapshot_sha256": snapshot.get("snapshot_sha256"),
            "versions": snapshot.get("versions"),
            "checksums": snapshot.get("checksums"),
            "replacement_mode": snapshot.get("replacement_mode"),
        }
        if snapshot
        else None,
        "business_error": report.get("business_error"),
    }
