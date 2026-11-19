"""Stage-by-stage numerical error propagation and attribution."""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np

from .deterministic import sha256_json
from .precision import (
    STAGE_NAMES,
    PrecisionExecution,
    verify_fixed_recomputation,
)


CONFIDENCE = 0.95
Z_95 = 1.959963984540054


def _finite_ratio(numerator: float, denominator: float, epsilon: float = 1e-300) -> float:
    if denominator <= epsilon:
        return 1.0
    return numerator / denominator


def _mean_interval(values: Iterable[float]) -> tuple[float, float]:
    array = np.asarray(list(values), dtype=np.float64)
    if array.size == 0:
        return 0.0, 0.0
    mean = float(np.mean(array))
    if array.size == 1:
        return max(0.0, mean), max(0.0, mean)
    sem = float(np.std(array, ddof=1) / math.sqrt(array.size))
    margin = Z_95 * sem
    return max(0.0, mean - margin), max(0.0, mean + margin)


def _summary_from_values(values: np.ndarray) -> dict[str, Any]:
    array = np.asarray(values, dtype=np.float64)
    if array.size == 0:
        return {
            "mean": 0.0,
            "max": 0.0,
            "signed_mean": 0.0,
            "interval_low": 0.0,
            "interval_high": 0.0,
        }
    low, high = _mean_interval(array.tolist())
    return {
        "mean": float(np.mean(array)),
        "max": float(np.max(array)),
        "signed_mean": float(np.mean(array)),
        "interval_low": low,
        "interval_high": high,
    }


def _frame_details(
    reference: np.ndarray,
    observed: np.ndarray,
    epsilon: float,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    reference_array = np.asarray(reference, dtype=np.float64)
    observed_array = np.asarray(observed, dtype=np.float64)
    if reference_array.shape != observed_array.shape:
        raise ValueError("reference and observed shapes differ")
    details: list[dict[str, Any]] = []
    all_abs: list[float] = []
    abs_sum = 0.0
    reference_abs_sum = 0.0
    for frame_index in range(reference_array.shape[0]):
        reference_row = reference_array[frame_index]
        observed_row = observed_array[frame_index]
        signed = observed_row - reference_row
        absolute = np.abs(signed)
        denominator = np.maximum(np.abs(reference_row), epsilon)
        relative_components = absolute / denominator
        component_interval_low, component_interval_high = _mean_interval(absolute.tolist())
        frame_abs = float(np.mean(absolute))
        all_abs.append(frame_abs)
        abs_sum += float(np.sum(absolute))
        reference_abs_sum += float(np.sum(np.abs(reference_row)))
        details.append(
            {
                "frame_index": frame_index,
                "abs_deviation": frame_abs,
                "relative_deviation": float(
                    np.mean(np.minimum(relative_components, 1e12))
                ),
                "signed_deviation": float(np.mean(signed)),
                "reference_mean": float(np.mean(reference_row)),
                "observed_mean": float(np.mean(observed_row)),
                "interval_low": component_interval_low,
                "interval_high": component_interval_high,
                "component_abs_deviations": [float(value) for value in absolute],
                "component_signed_deviations": [float(value) for value in signed],
            }
        )
    value_summary = _summary_from_values(np.asarray(all_abs, dtype=np.float64))
    frame_summary = {
        "abs_deviation_mean": value_summary["mean"],
        "abs_deviation_max": value_summary["max"],
        "signed_deviation_mean": value_summary["signed_mean"],
        "relative_deviation": float(
            abs_sum / (reference_abs_sum + epsilon)
        ),
        "error_interval": {
            "low": value_summary["interval_low"],
            "high": value_summary["interval_high"],
            "confidence": CONFIDENCE,
            "method": "normal_ci_of_mean_frame_abs_deviation",
        },
        "component_count_per_frame": int(reference_array.shape[1])
        if reference_array.ndim == 2
        else 1,
    }
    candidate_summary = {
        "abs_deviation_mean": float(np.mean(np.abs(observed_array - reference_array))),
        "abs_deviation_max": float(np.max(np.abs(observed_array - reference_array))),
        "relative_deviation": float(
            np.sum(np.abs(observed_array - reference_array))
            / (np.sum(np.abs(reference_array)) + epsilon)
        ),
    }
    return details, frame_summary, candidate_summary


def _stage_deviation(
    reference_trace: Any,
    observed_trace: Any,
    epsilon: float,
) -> dict[str, Any]:
    details, frame_summary, frame_candidate_summary = _frame_details(
        reference_trace.frame_values,
        observed_trace.frame_values,
        epsilon,
    )
    candidate_combined: dict[str, Any] = {
        "abs_deviation_mean": 0.0,
        "abs_deviation_max": 0.0,
        "relative_deviation": 0.0,
    }
    if (
        reference_trace.candidate_values is not None
        and observed_trace.candidate_values is not None
    ):
        candidate_combined = _frame_details(
            reference_trace.candidate_values,
            observed_trace.candidate_values,
            epsilon,
        )[2]
    combined_mean = float(
        np.mean(
            [
                frame_summary["abs_deviation_mean"],
                candidate_combined["abs_deviation_mean"],
            ]
        )
    )
    combined_max = max(
        frame_summary["abs_deviation_max"],
        candidate_combined["abs_deviation_max"],
    )
    return {
        "name": observed_trace.name,
        "abs_deviation_mean": combined_mean,
        "abs_deviation_max": combined_max,
        "frame_abs_deviation_mean": frame_summary["abs_deviation_mean"],
        "frame_abs_deviation_max": frame_summary["abs_deviation_max"],
        "signed_deviation_mean": frame_summary["signed_deviation_mean"],
        "relative_deviation": frame_summary["relative_deviation"],
        "error_interval": frame_summary["error_interval"],
        "candidate_abs_deviation_mean": candidate_combined["abs_deviation_mean"],
        "candidate_abs_deviation_max": candidate_combined["abs_deviation_max"],
        "candidate_relative_deviation": candidate_combined["relative_deviation"],
        "component_count_per_frame": frame_summary["component_count_per_frame"],
        "quantization_residual_max": observed_trace.quantization_residual_max,
        "details": details,
        "detail_digest": sha256_json(details),
    }


def compute_error_spectrum(
    reference: PrecisionExecution,
    observed: PrecisionExecution,
    *,
    truth_observation: dict[str, Any] | None = None,
    epsilon: float = 1e-12,
) -> dict[str, Any]:
    if reference.tier != "float64":
        raise ValueError("reference execution must be float64")
    if reference.frame_set_digest != observed.frame_set_digest:
        raise ValueError("frame sets differ")
    if reference.assignment_digest != observed.assignment_digest:
        raise ValueError("assignment sets differ")
    stages: dict[str, Any] = {}
    previous_abs = 0.0
    first_abs: float | None = None
    for name in STAGE_NAMES:
        stage = _stage_deviation(
            reference.stages[name],
            observed.stages[name],
            epsilon,
        )
        current_abs = float(stage["abs_deviation_mean"])
        if first_abs is None:
            first_abs = current_abs
        stage["amplification_ratio"] = _finite_ratio(current_abs, previous_abs)
        stage["amplification_ratio_vs_previous"] = stage["amplification_ratio"]
        stage["amplification_ratio_vs_first"] = _finite_ratio(current_abs, first_abs or 0.0)
        stage["previous_abs_deviation_mean"] = previous_abs
        stages[name] = stage
        previous_abs = current_abs
    dominant_name = max(
        STAGE_NAMES,
        key=lambda name: (stages[name]["abs_deviation_mean"], -STAGE_NAMES.index(name)),
    )
    evidence = sorted(
        stages[dominant_name]["details"],
        key=lambda row: (-row["abs_deviation"], row["frame_index"]),
    )[:3]
    dominant = {
        "stage": dominant_name,
        "metric": "abs_deviation_mean",
        "value": stages[dominant_name]["abs_deviation_mean"],
        "amplification_ratio_vs_previous": stages[dominant_name][
            "amplification_ratio_vs_previous"
        ],
        "evidence_frames": [
            {
                "frame_index": row["frame_index"],
                "abs_deviation": row["abs_deviation"],
                "relative_deviation": row["relative_deviation"],
                "interval_low": row["interval_low"],
                "interval_high": row["interval_high"],
                "component_abs_deviations": row["component_abs_deviations"],
            }
            for row in evidence
        ],
        "rationale": "largest mean absolute deviation over frame and candidate tensors; evidence_frames locate the largest frame-level contributions",
    }
    fixed = verify_fixed_recomputation(observed)
    return {
        "reference_tier": "float64",
        "observed_tier": observed.tier,
        "observed_precision": observed.config.to_dict(),
        "frame_count": len(observed.frame_indices),
        "frame_indices": [int(value) for value in observed.frame_indices],
        "stages": stages,
        "dominant_source": dominant,
        "fixed_recompute": fixed,
        "degraded": any(stages[name]["abs_deviation_mean"] > 0.0 for name in STAGE_NAMES),
        "truth_observation": truth_observation,
        "definitions": {
            "absolute_deviation": "mean absolute difference between the tier stage tensor and the float64 stage tensor at the same frame/component",
            "relative_deviation": "sum absolute frame deviation divided by sum absolute float64 frame reference",
            "error_interval": "95% normal confidence interval for the mean frame absolute deviation",
            "amplification_ratio": "current stage mean absolute deviation divided by previous stage mean absolute deviation; first stage uses 1.0",
            "candidate_tensor": "the speaker x phoneme candidate vectors at feature-scale, normalization and log-transform stages",
        },
    }


def _aggregate_truth_observations(
    observations: Iterable[dict[str, Any] | None],
) -> dict[str, Any] | None:
    present = [value for value in observations if value is not None]
    if not present:
        return None
    frames = sum(int(value["summary"].get("frame_count", 0)) for value in present)
    if frames == 0:
        counts = [
            len(value["observations"]["frame_index"])
            for value in present
            if "observations" in value
        ]
        frames = sum(counts)
    weighted: dict[str, float] = {}
    keys = (
        "f0_mae_hz",
        "f0_relative_mae",
        "energy_mae",
        "energy_relative_mae",
        "combined_relative_mae",
    )
    for key in keys:
        weighted[key] = 0.0
    for value in present:
        count = len(value["observations"]["frame_index"])
        for key in keys:
            weighted[key] += float(value["summary"][key]) * count
    if frames > 0:
        for key in keys:
            weighted[key] /= frames
    detailed: list[dict[str, Any]] = []
    for value in present:
        detailed.append(
            {
                "utterance_id": value.get("utterance_id"),
                "observations": value.get("observations", {}),
                "summary": value.get("summary", {}),
            }
        )
    first = present[0]
    return {
        "reference_tier": "float64",
        "summary": {**weighted, "frame_count": frames},
        "definition": first.get("definition", {}),
        "by_utterance": detailed,
    }


def aggregate_error_spectra(
    spectra: Iterable[dict[str, Any]],
    *,
    epsilon: float = 1e-12,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    truth_values: list[dict[str, Any] | None] = []
    for spectrum in spectra:
        grouped.setdefault(str(spectrum["observed_tier"]), []).append(spectrum)
        truth_values.append(spectrum.get("truth_observation"))
    tier_reports: dict[str, Any] = {}
    for tier in sorted(grouped):
        entries = grouped[tier]
        stages: dict[str, Any] = {}
        previous_abs = 0.0
        first_abs: float | None = None
        for name in STAGE_NAMES:
            details: list[dict[str, Any]] = []
            candidate_means: list[float] = []
            candidate_max: list[float] = []
            residual: list[float] = []
            quantization_residual: list[float] = []
            for entry in entries:
                stage = entry["stages"][name]
                for row in stage["details"]:
                    item = dict(row)
                    item["utterance_id"] = entry.get("utterance_id")
                    details.append(item)
                candidate_means.append(float(stage["candidate_abs_deviation_mean"]))
                candidate_max.append(float(stage["candidate_abs_deviation_max"]))
                quantization_residual.append(float(stage.get("quantization_residual_max", 0.0)))
            abs_values = np.asarray([row["abs_deviation"] for row in details], dtype=np.float64)
            interval_low, interval_high = _mean_interval(abs_values.tolist())
            reference_total = 0.0
            abs_total = 0.0
            for row in details:
                reference_total += float(row["reference_mean"]) * max(
                    1, len(row["component_abs_deviations"])
                )
                abs_total += float(row["abs_deviation"]) * max(
                    1, len(row["component_abs_deviations"])
                )
            frame_abs_mean = float(np.mean(abs_values)) if abs_values.size else 0.0
            candidate_mean = float(np.mean(candidate_means)) if candidate_means else 0.0
            combined_mean = float((frame_abs_mean + candidate_mean) / 2.0)
            if first_abs is None:
                first_abs = combined_mean
            stage_report = {
                "name": name,
                "abs_deviation_mean": combined_mean,
                "abs_deviation_max": max(
                    ([float(np.max(abs_values))] if abs_values.size else [0.0])
                    + candidate_max
                ),
                "frame_abs_deviation_mean": frame_abs_mean,
                "frame_abs_deviation_max": float(np.max(abs_values))
                if abs_values.size
                else 0.0,
                "signed_deviation_mean": float(np.mean([row["signed_deviation"] for row in details]))
                if details
                else 0.0,
                "relative_deviation": float(
                    abs_total / (reference_total + epsilon)
                ),
                "error_interval": {
                    "low": interval_low,
                    "high": interval_high,
                    "confidence": CONFIDENCE,
                    "method": "normal_ci_of_mean_frame_abs_deviation",
                },
                "candidate_abs_deviation_mean": candidate_mean,
                "candidate_abs_deviation_max": float(np.max(candidate_max))
                if candidate_max
                else 0.0,
                "amplification_ratio": _finite_ratio(combined_mean, previous_abs),
                "amplification_ratio_vs_previous": _finite_ratio(combined_mean, previous_abs),
                "amplification_ratio_vs_first": _finite_ratio(
                    combined_mean, first_abs or 0.0
                ),
                "previous_abs_deviation_mean": previous_abs,
                "quantization_residual_max": float(np.max(quantization_residual))
                if quantization_residual
                else 0.0,
                "detail_digest": sha256_json(details),
                "details": details,
            }
            stages[name] = stage_report
            previous_abs = combined_mean
        dominant_name = max(
            STAGE_NAMES,
            key=lambda name: (
                stages[name]["abs_deviation_mean"],
                -STAGE_NAMES.index(name),
            ),
        )
        evidence = sorted(
            stages[dominant_name]["details"],
            key=lambda row: (-row["abs_deviation"], str(row.get("utterance_id")), row["frame_index"]),
        )[:3]
        tier_reports[tier] = {
            "reference_tier": "float64",
            "observed_tier": tier,
            "frame_count": len(
                {
                    (row.get("utterance_id"), row["frame_index"])
                    for row in stages["feature_scale"]["details"]
                }
            ),
            "stages": stages,
            "dominant_source": {
                "stage": dominant_name,
                "metric": "abs_deviation_mean",
                "value": stages[dominant_name]["abs_deviation_mean"],
                "amplification_ratio_vs_previous": stages[dominant_name][
                    "amplification_ratio_vs_previous"
                ],
                "evidence_frames": [
                    {
                        "utterance_id": row.get("utterance_id"),
                        "frame_index": row["frame_index"],
                        "abs_deviation": row["abs_deviation"],
                        "relative_deviation": row["relative_deviation"],
                        "component_abs_deviations": row["component_abs_deviations"],
                    }
                    for row in evidence
                ],
                "rationale": "largest mean absolute deviation; evidence_frames point to detailed component deviations",
            },
            "degraded": any(
                stages[name]["abs_deviation_mean"] > 0.0 for name in STAGE_NAMES
            ),
            "fixed_recompute": (
                entries[0].get("fixed_recompute") if entries and tier == "fixed" else {
                    "ok": True,
                    "applicable": False,
                }
            ),
        }
    return {
        "reference_tier": "float64",
        "tiers": tier_reports,
        "truth_observation": _aggregate_truth_observations(truth_values),
        "definitions": {
            "frame_universe": "all (utterance_id, frame_index) pairs retained across every tier",
            "dominant_source": "stage with the largest combined frame/candidate mean absolute deviation",
            "truth_observation": "f0_true in Hz and energy_true as unwindowed mean square PCM over exact frame samples",
        },
    }
