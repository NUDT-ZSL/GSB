"""Error spectrum, tolerance adjudication and stage attribution."""

from __future__ import annotations

from typing import Any

import numpy as np

from .models import OperatorConfig
from .errors import SparseOperatorError
from .codes import E_CONFIG_INVALID


_UNIT_ROUNDOFF = float(np.finfo(np.float64).eps)


def _position_payload(
    flat_index: int,
    shape: tuple[int, ...],
    *,
    reference: np.ndarray,
    actual: np.ndarray,
    abs_error: np.ndarray,
    relative: np.ndarray | None,
) -> dict[str, Any]:
    index = np.unravel_index(flat_index, shape)
    payload: dict[str, Any] = {
        "flat_index": int(flat_index),
        "index": [int(item) for item in index],
        "reference": float(reference[index]),
        "actual": float(actual[index]),
        "abs_error": float(abs_error[index]),
    }
    if len(shape) == 3:
        payload["batch"] = int(index[0])
        payload["row"] = int(index[1])
        payload["col"] = int(index[2])
    elif len(shape) == 2:
        payload["row"] = int(index[0])
        payload["col"] = int(index[1])
    if relative is not None:
        value = float(relative[index])
        payload["relative_error"] = value if np.isfinite(value) else None
    return payload


def _quantiles(values: np.ndarray) -> dict[str, float]:
    if values.size == 0:
        return {"p0": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0, "p100": 0.0}
    points = np.quantile(values, [0.0, 0.5, 0.9, 0.99, 1.0], method="linear")
    labels = ("p0", "p50", "p90", "p99", "p100")
    return {label: float(value) for label, value in zip(labels, points)}


def diagnose_error_spectrum(
    reference: np.ndarray,
    actual: np.ndarray,
    config: OperatorConfig | dict[str, Any] | None = None,
    *,
    case_id: str | None = None,
    stage_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(config, dict):
        config = OperatorConfig.from_mapping(config)
    elif config is None:
        config = OperatorConfig()
    elif not isinstance(config, OperatorConfig):
        raise SparseOperatorError(
            E_CONFIG_INVALID,
            "config must be an OperatorConfig or JSON object",
            phase="configuration",
            field="config",
        )
    ref = np.asarray(reference, dtype=np.float64)
    got = np.asarray(actual, dtype=np.float64)
    if ref.shape != got.shape:
        raise ValueError("reference and actual shapes must match")

    finite_actual = np.isfinite(got)
    nonfinite_count = int(got.size - np.count_nonzero(finite_actual))
    safe_actual = np.where(finite_actual, got, ref)
    abs_error = np.abs(safe_actual - ref)
    allowed = config.atol + config.rtol * np.abs(ref)
    violation_mask = (abs_error > allowed) | (~finite_actual)
    violation_count = int(np.count_nonzero(violation_mask))
    relative = np.full_like(abs_error, np.nan)
    np.divide(abs_error, np.abs(ref), out=relative, where=ref != 0.0)
    relative_defined = np.isfinite(relative)

    flat_abs = abs_error.reshape(-1)
    max_abs_error = float(flat_abs.max()) if flat_abs.size else 0.0
    max_abs_index = int(np.argmax(flat_abs)) if flat_abs.size else 0
    max_abs_position = (
        _position_payload(
            max_abs_index,
            ref.shape,
            reference=ref,
            actual=safe_actual,
            abs_error=abs_error,
            relative=relative,
        )
        if flat_abs.size
        else None
    )

    max_relative = None
    max_relative_position = None
    if np.any(relative_defined):
        relative_for_index = np.where(relative_defined, relative, -1.0)
        max_relative_index = int(np.argmax(relative_for_index))
        max_relative = float(relative.reshape(-1)[max_relative_index])
        max_relative_position = _position_payload(
            max_relative_index,
            ref.shape,
            reference=ref,
            actual=safe_actual,
            abs_error=abs_error,
            relative=relative,
        )

    worst_violations: list[dict[str, Any]] = []
    if violation_count:
        violation_magnitude = np.where(violation_mask, abs_error - allowed, -np.inf)
        order = np.argsort(-violation_magnitude.reshape(-1), kind="stable")
        for flat_index in order[: config.max_reported_violations]:
            if not np.isfinite(violation_magnitude.reshape(-1)[flat_index]):
                continue
            payload = _position_payload(
                int(flat_index),
                ref.shape,
                reference=ref,
                actual=safe_actual,
                abs_error=abs_error,
                relative=relative,
            )
            payload["allowed_error"] = float(allowed.reshape(-1)[flat_index])
            payload["excess"] = float(violation_magnitude.reshape(-1)[flat_index])
            worst_violations.append(payload)

    max_abs_reference = float(np.max(np.abs(ref))) if ref.size else 0.0
    roundoff_floor = _UNIT_ROUNDOFF * max(1.0, max_abs_reference)
    amplification = max_abs_error / roundoff_floor if roundoff_floor else 0.0

    metrics = dict(stage_metrics or {})
    row_block_metric = {
        "value": 0.0,
        "position": None,
        "method": "disjoint row chunks do not share output elements",
    }
    if "row_block" in metrics and isinstance(metrics["row_block"], dict):
        row_block_metric = metrics["row_block"]
    metric_values = {
        "sparse_index_summation": float(
            metrics.get("sparse_index_summation", {}).get("value", 0.0)
        ),
        "cross_block_reduction": float(
            metrics.get("cross_block_reduction", {}).get("value", 0.0)
        ),
        "row_block": float(row_block_metric.get("value", 0.0)),
    }
    stage_priority = {
        "sparse_index_summation": 2,
        "cross_block_reduction": 1,
        "row_block": 0,
    }
    if max(metric_values.values()) > 0.0:
        dominant_source = max(
            metric_values,
            key=lambda name: (metric_values[name], stage_priority[name]),
        )
    else:
        dominant_source = "none"

    return {
        "case_id": case_id,
        "truth_source": "dense float64 reference reconstructed from the same seed/metadata",
        "tolerance": {
            "formula": "abs_error <= atol + rtol * abs(reference)",
            "atol": config.atol,
            "rtol": config.rtol,
        },
        "passed": violation_count == 0,
        "nonfinite_element_count": nonfinite_count,
        "violation_count": violation_count,
        "max_abs_error": max_abs_error,
        "max_abs_error_position": max_abs_position,
        "max_relative_error": max_relative,
        "max_relative_error_position": max_relative_position,
        "abs_error_mean": float(np.mean(flat_abs)) if flat_abs.size else 0.0,
        "abs_error_rms": (
            float(np.sqrt(np.mean(np.square(flat_abs))))
            if flat_abs.size
            else 0.0
        ),
        "relative_defined_element_count": int(np.count_nonzero(relative_defined)),
        "abs_error_quantiles": _quantiles(flat_abs),
        "relative_error_quantiles": (
            _quantiles(relative[relative_defined])
            if np.any(relative_defined)
            else None
        ),
        "reference_scale": max_abs_reference,
        "roundoff_floor": roundoff_floor,
        "error_amplification_ratio": amplification,
        "amplification_definition": (
            "max_abs_error / (float64_eps * max(1, max_abs(reference)))"
        ),
        "worst_violations": worst_violations,
        "stage_attribution": {
            "dominant_source": dominant_source,
            "method": (
                "isolated local sparse residuals, isolated cross-tile merge "
                "rounding, and disjoint row-partition effect; all measured from "
                "reconstructable dense float64 tile references"
            ),
            "metrics": {
                "sparse_index_summation": metrics.get(
                    "sparse_index_summation",
                    {"value": 0.0, "position": None, "method": "not observed"},
                ),
                "cross_block_reduction": metrics.get(
                    "cross_block_reduction",
                    {"value": 0.0, "position": None, "method": "not observed"},
                ),
                "row_block": row_block_metric,
            },
        },
    }

