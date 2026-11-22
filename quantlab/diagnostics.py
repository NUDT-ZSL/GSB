from __future__ import annotations

from typing import Any

import numpy as np

from .config import WorkbenchConfig
from .deterministic import generate_weights
from .errors import BusinessError, E_TOLERANCE_EXCEEDED
from .inference import (
    run_block_isolated,
    run_full_path,
)
from .quantization import (
    QuantizedLayer,
    single_scale_dequantize,
    verify_dequantized_weights,
)
from .serialization import array_checksum


ERROR_LEVELS = (
    "quantization_rounding",
    "group_scaling",
    "block_accumulation",
    "layer_cascade",
)


def _stats(actual: np.ndarray, reference: np.ndarray) -> dict[str, Any]:
    error = np.asarray(actual, dtype=np.float64) - np.asarray(reference, dtype=np.float64)
    abs_error = np.abs(error)
    denominator = np.abs(reference) + np.finfo(np.float64).eps
    signed_min = float(np.min(error)) if error.size else 0.0
    signed_max = float(np.max(error)) if error.size else 0.0
    return {
        "max_abs": float(np.max(abs_error)) if abs_error.size else 0.0,
        "mean_abs": float(np.mean(abs_error)) if abs_error.size else 0.0,
        "rmse": float(np.sqrt(np.mean(np.square(error)))) if error.size else 0.0,
        "mean_relative": float(np.mean(abs_error / denominator)) if error.size else 0.0,
        "max_relative": float(np.max(abs_error / denominator)) if abs_error.size else 0.0,
        "interval": [min(signed_min, signed_max), max(signed_min, signed_max)],
        "element_count": int(error.size),
    }


def _amplification(current: float, base: float) -> float:
    if base <= np.finfo(np.float64).eps:
        return 1.0 if current <= np.finfo(np.float64).eps else float(current / np.finfo(np.float64).eps)
    return float(current / base)


def _first_exceed(
    actual: np.ndarray,
    reference: np.ndarray,
    tolerance: float,
) -> tuple[tuple[int, ...] | None, float]:
    abs_error = np.abs(np.asarray(actual) - np.asarray(reference))
    bad = np.argwhere(abs_error > tolerance)
    if bad.size == 0:
        return None, float(np.max(abs_error)) if abs_error.size else 0.0
    first = tuple(int(x) for x in bad[0])
    return first, float(abs_error[first])


def build_diagnostics(
    config: WorkbenchConfig,
    entries: list[dict[str, Any]],
    records: dict[str, QuantizedLayer],
    *,
    initial: np.ndarray,
    operational_activations: dict[str, np.ndarray],
    operational_prediction: np.ndarray,
) -> dict[str, Any]:
    reference = run_full_path(
        entries,
        {layer_id: record.dequantized for layer_id, record in records.items()},
        initial,
    )
    original_weights = {
        str(entry["layer_id"]): generate_weights(config, entry) for entry in entries
    }
    original_path = run_full_path(entries, original_weights, initial)
    single_scale_weights = {
        str(entry["layer_id"]): single_scale_dequantize(original_weights[str(entry["layer_id"])], entry)
        for entry in entries
    }
    single_scale_path = run_full_path(entries, single_scale_weights, initial)
    block_isolated = run_block_isolated(
        entries,
        {layer_id: record.dequantized for layer_id, record in records.items()},
        reference,
        initial,
        max_block_elements=config.max_block_elements,
    )
    path_by_level = {
        "quantization_rounding": original_path,
        "group_scaling": single_scale_path,
        "block_accumulation": block_isolated,
        "layer_cascade": operational_activations,
    }
    layer_spectrum: list[dict[str, Any]] = []
    first_by_level: dict[str, float] = {}
    previous_by_level: dict[str, float] = {}
    tolerance_violation: dict[str, Any] | None = None
    for entry in entries:
        layer_id = str(entry["layer_id"])
        layer_index = int(entry["layer_index"])
        ref = reference[layer_id]
        levels: list[dict[str, Any]] = []
        for level in ERROR_LEVELS:
            actual = path_by_level[level][layer_id]
            stats = _stats(actual, ref)
            mean_abs = stats["mean_abs"]
            first_by_level.setdefault(level, mean_abs)
            stats["amplification_ratio"] = _amplification(
                mean_abs, first_by_level[level]
            )
            stats["step_amplification_ratio"] = _amplification(
                mean_abs, previous_by_level.get(level, mean_abs)
            )
            previous_by_level[level] = mean_abs
            stats["level"] = level
            stats["interval_valid"] = stats["interval"][0] <= stats["interval"][1]
            levels.append(stats)
            if tolerance_violation is None:
                first_index, actual_max = _first_exceed(
                    actual, ref, float(config.tolerance_abs)
                )
                if first_index is not None:
                    tolerance_violation = {
                        "layer_id": layer_id,
                        "layer_index": layer_index,
                        "level": level,
                        "first_element_index": list(first_index),
                        "max_abs": actual_max,
                        "tolerance_abs": float(config.tolerance_abs),
                    }
        layer_spectrum.append(
            {
                "layer_id": layer_id,
                "layer_index": layer_index,
                "reference_checksum": array_checksum(ref),
                "levels": levels,
            }
        )

    level_summary: dict[str, dict[str, Any]] = {}
    for level in ERROR_LEVELS:
        means = [
            next(item for item in layer["levels"] if item["level"] == level)["mean_abs"]
            for layer in layer_spectrum
        ]
        maxes = [
            next(item for item in layer["levels"] if item["level"] == level)["max_abs"]
            for layer in layer_spectrum
        ]
        intervals = [
            next(item for item in layer["levels"] if item["level"] == level)["interval"]
            for layer in layer_spectrum
        ]
        level_summary[level] = {
            "mean_abs": float(np.mean(means)) if means else 0.0,
            "max_abs": float(np.max(maxes)) if maxes else 0.0,
            "interval": [
                float(min(interval[0] for interval in intervals)) if intervals else 0.0,
                float(max(interval[1] for interval in intervals)) if intervals else 0.0,
            ],
            "amplification_ratio": _amplification(
                means[-1] if means else 0.0, means[0] if means else 0.0
            ),
        }
    dominant_level = max(
        ERROR_LEVELS,
        key=lambda level: (level_summary[level]["mean_abs"], level_summary[level]["max_abs"]),
    )
    intervals_legal = all(
        item["interval_valid"]
        for layer in layer_spectrum
        for item in layer["levels"]
    ) and all(
        summary["interval"][0] <= summary["interval"][1]
        for summary in level_summary.values()
    )
    fixed_point_checks = [
        verify_dequantized_weights(records[str(entry["layer_id"])], original_weights[str(entry["layer_id"])])
        for entry in entries
    ]
    if tolerance_violation is not None:
        raise BusinessError(
            E_TOLERANCE_EXCEEDED,
            (
                f"层 {tolerance_violation['layer_index']} ({tolerance_violation['layer_id']}) "
                f"在 {tolerance_violation['level']} 超出容差"
            ),
            stage="diagnostics",
            layer_id=tolerance_violation["layer_id"],
            layer_index=tolerance_violation["layer_index"],
            details=tolerance_violation,
        )
    prediction_reference = np.ascontiguousarray(np.mean(reference[str(entries[-1]["layer_id"])], axis=0))
    return {
        "reference": "float64 dequantized weights, full GEMM, tanh",
        "operational": "float64 dequantized weights, blocked GEMM, tanh",
        "rounding_mode": "rint_half_even",
        "error_spectrum": layer_spectrum,
        "level_summary": level_summary,
        "dominant_source": dominant_level,
        "dominant_source_detail": level_summary[dominant_level],
        "intervals_legal": intervals_legal,
        "fixed_point_recompute": fixed_point_checks,
        "fixed_point_all_match": all(
            item["codes_match"]
            and item["dequantized_from_codes_match"]
            and item["independent_recompute_match"]
            for item in fixed_point_checks
        ),
        "prediction": {
            "operational": operational_prediction.tolist(),
            "reference": prediction_reference.tolist(),
            "operational_checksum": array_checksum(operational_prediction),
            "reference_checksum": array_checksum(prediction_reference),
            "max_abs_error": float(np.max(np.abs(operational_prediction - prediction_reference))),
        },
        "tolerance": {
            "abs": float(config.tolerance_abs),
            "rel": float(config.tolerance_rel),
            "passed": True,
        },
    }
