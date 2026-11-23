from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config import WorkbenchConfig
from .precision import PrecisionOps


def _flatten(values: Any) -> list[float]:
    return [
        float(value)
        for value in np.asarray(values, dtype=np.float64).reshape(-1).tolist()
    ]


def _summary(
    reference_values: list[float],
    candidate_values: list[float],
    *,
    unit: float,
    upstream: float,
) -> dict[str, Any]:
    if len(reference_values) != len(candidate_values):
        raise ValueError("deviation details have mismatched lengths")
    signed = [
        float(candidate) - float(reference)
        for reference, candidate in zip(reference_values, candidate_values)
    ]
    absolute = [abs(value) for value in signed]
    relative = [
        abs_value / max(abs(float(reference)), 1e-300)
        for abs_value, reference in zip(absolute, reference_values)
    ]
    max_abs = max(absolute, default=0.0)
    max_rel = max(relative, default=0.0)
    divisor = max(float(unit), float(upstream), 1e-300)
    return {
        "max_absolute_deviation": float(max_abs),
        "max_relative_deviation": float(max_rel),
        "error_interval": {
            "min_signed_deviation": float(min(signed, default=0.0)),
            "max_signed_deviation": float(max(signed, default=0.0)),
        },
        "error_amplification_ratio": float(max_abs / divisor),
        "sample_count": len(reference_values),
    }


def _stage(
    name: str,
    reference_values: Any,
    candidate_values: Any,
    *,
    unit: float,
    upstream: float,
    notes: str,
) -> dict[str, Any]:
    reference = _flatten(reference_values)
    candidate = _flatten(candidate_values)
    details = {
        "reference_values": reference,
        "candidate_values": candidate,
        "unit": float(unit),
        "upstream_error": float(upstream),
    }
    summary = _summary(
        reference, candidate, unit=unit, upstream=upstream
    )
    return {
        "stage": name,
        "summary": summary,
        "details": details,
        "notes": notes,
    }


def recompute_stage(stage: dict[str, Any]) -> dict[str, Any]:
    details = stage["details"]
    return _summary(
        [float(value) for value in details["reference_values"]],
        [float(value) for value in details["candidate_values"]],
        unit=float(details["unit"]),
        upstream=float(details["upstream_error"]),
    )


def _batch_outputs(batch: dict[str, Any]) -> np.ndarray:
    rows = batch["rows"]
    if batch.get("model_family") == "gmm" or (
        rows and rows[0].get("model_family") == "gmm"
    ):
        values: list[float] = []
        for row in rows:
            values.extend(float(value) for value in row["responsibilities"])
            values.append(float(row["density"]))
        return np.asarray(values, dtype=np.float64)
    values = []
    for row in rows:
        values.append(float(row["density"]))
        values.append(float(row.get("raw_chunk_sum", 0.0)))
    return np.asarray(values, dtype=np.float64)


def _convergence_vector(batch: dict[str, Any]) -> list[float]:
    trace = batch.get("ll_trace") or []
    convergence = batch.get("convergence") or {}
    last_ll = float(trace[-1]["log_likelihood"]) if trace else 0.0
    return [
        last_ll,
        float(convergence.get("iteration", 0)),
        1.0 if convergence.get("converged", False) else 0.0,
    ]


def build_error_spectrum(
    *,
    candidate_config: WorkbenchConfig,
    candidate_ops: PrecisionOps,
    candidate_normalized: np.ndarray,
    candidate_batch: dict[str, Any],
    reference_config: WorkbenchConfig,
    reference_ops: PrecisionOps,
    reference_normalized: np.ndarray,
    reference_batch: dict[str, Any],
) -> dict[str, Any]:
    candidate_norm = np.asarray(candidate_normalized, dtype=np.float64)
    reference_norm = np.asarray(reference_normalized, dtype=np.float64)
    if candidate_norm.shape != reference_norm.shape:
        raise ValueError("normalized matrices have different shapes")
    quantized_reference = candidate_ops.quantize(reference_norm)
    quantization_stage = _stage(
        "quantization_rounding",
        reference_norm,
        quantized_reference,
        unit=candidate_ops.quantization_bound(),
        upstream=0.0,
        notes=(
            "Reference float64 standardized values passed through the candidate "
            "precision quantizer; isolates representation/rounding error."
        ),
    )
    standardization_stage = _stage(
        "standardization",
        reference_norm,
        candidate_norm,
        unit=candidate_ops.quantization_bound(),
        upstream=quantization_stage["summary"]["max_absolute_deviation"],
        notes=(
            "Full standardized matrices, float64 reference versus candidate "
            "precision pipeline."
        ),
    )
    candidate_output = _batch_outputs(candidate_batch)
    reference_output = _batch_outputs(reference_batch)
    chunk_stage = _stage(
        "chunk_reduction",
        reference_output,
        candidate_output,
        unit=candidate_ops.quantization_bound(),
        upstream=standardization_stage["summary"]["max_absolute_deviation"],
        notes=(
            "Final density/responsibility output after explicit ordered chunk "
            "reduction, float64 reference versus candidate."
        ),
    )
    convergence_stage = _stage(
        "convergence_decision",
        _convergence_vector(reference_batch),
        _convergence_vector(candidate_batch),
        unit=max(float(candidate_config.tol), candidate_ops.quantization_bound()),
        upstream=chunk_stage["summary"]["max_absolute_deviation"],
        notes=(
            "Final log-likelihood, selected iteration and convergence flag."
        ),
    )

    unit = max(candidate_ops.quantization_bound(), 1e-300)
    scores = {
        "chunk_reduction": float(
            chunk_stage["summary"]["max_absolute_deviation"]
            / max(
                unit,
                standardization_stage["summary"]["max_absolute_deviation"],
            )
        ),
        "quantization_rounding": float(
            quantization_stage["summary"]["max_absolute_deviation"] / unit
        ),
        "convergence_decision": float(
            convergence_stage["summary"]["max_absolute_deviation"]
            / max(float(candidate_config.tol), unit)
        ),
    }
    dominant = max(scores, key=lambda name: (scores[name], name))
    if max(scores.values()) == 0.0:
        dominant = "chunk_reduction"
        reason = "all_measured_stage_deviations_are_zero"
    elif scores[dominant] == 0.0:
        reason = "zero_candidate_deviation"
    else:
        reason = "largest_normalized_stage_score"
    return {
        "schema_version": 1,
        "reference_precision": "float64",
        "candidate_precision": candidate_config.precision,
        "candidate_quantization_unit": candidate_ops.quantization_bound(),
        "stages": [
            standardization_stage,
            chunk_stage,
            quantization_stage,
            convergence_stage,
        ],
        "localization": {
            "dominant_source": dominant,
            "scores": scores,
            "reason": reason,
            "allowed_sources": [
                "chunk_reduction",
                "quantization_rounding",
                "convergence_decision",
            ],
        },
        "reference_batch": {
            "checksum": reference_batch.get("checksum"),
            "precision": reference_batch.get("precision"),
        },
        "candidate_batch": {
            "checksum": candidate_batch.get("checksum"),
            "precision": candidate_batch.get("precision"),
        },
        "recompute_contract": {
            "formula": (
                "max_abs=max(abs(candidate-reference)); "
                "max_rel=max(abs(candidate-reference)/max(abs(reference),1e-300)); "
                "interval=[min(signed),max(signed)]; "
                "amplification=max_abs/max(unit,upstream,1e-300)"
            ),
            "details_required": [
                "reference_values",
                "candidate_values",
                "unit",
                "upstream_error",
            ],
        },
    }
