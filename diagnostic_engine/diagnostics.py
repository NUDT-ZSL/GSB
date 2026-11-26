from __future__ import annotations

from typing import Any

import numpy as np

from .blocking import BlockPlan
from .config import EngineConfig
from .precision import PrecisionOps
from .solver import PathRun, compute_gradient
from .utils import sha256_json


LEVEL_ORDER = (
    "feature_scaling",
    "block_accumulation",
    "fixed_quantization",
    "gradient_update",
)


def _deviation_record(
    *,
    level: str,
    reference_value: float,
    observed_value: float,
    quantum: float,
    details: dict[str, Any],
) -> dict[str, Any]:
    absolute = abs(float(observed_value) - float(reference_value))
    relative = absolute / max(abs(float(reference_value)), 1e-15)
    amplification = absolute / max(quantum, 1e-300)
    return {
        "level": level,
        "reference_value": float(reference_value),
        "observed_value": float(observed_value),
        "absolute_deviation": absolute,
        "relative_deviation": relative,
        "error_interval": [
            float(reference_value) - absolute,
            float(reference_value) + absolute,
        ],
        "quantization_unit": quantum,
        "amplification_ratio": amplification,
        "details": details,
    }


def build_error_spectrum(
    *,
    config: EngineConfig,
    reference: PathRun,
    observed: PathRun,
    labels: np.ndarray,
    block_plan: BlockPlan,
) -> dict[str, Any]:
    observed_ops = PrecisionOps.from_config(config, observed.precision)
    reference_ops = PrecisionOps.from_config(config, "float64")
    reference_matrix = reference.matrix
    observed_matrix = observed.matrix
    feature_abs = np.abs(observed_matrix - reference_matrix)
    feature_max = float(np.max(feature_abs)) if feature_abs.size else 0.0
    feature_ref = (
        float(np.max(np.abs(reference_matrix))) if reference_matrix.size else 0.0
    )
    feature_observed = (
        float(reference_matrix[np.unravel_index(np.argmax(feature_abs), feature_abs.shape)])
        if feature_abs.size
        else 0.0
    )
    feature_record = _deviation_record(
        level="feature_scaling",
        reference_value=feature_ref,
        observed_value=feature_observed,
        quantum=observed_ops.quantum,
        details={
            "max_abs_reference": feature_ref,
            "max_abs_deviation": feature_max,
            "matrix_sha256": sha256_json(observed_matrix.tolist()),
        },
    )
    # feature_observed is the reference value at the largest-deviation position;
    # use an explicit synthetic observed value to make the interval meaningful.
    feature_record["observed_value"] = float(feature_observed + feature_max)
    feature_record["absolute_deviation"] = feature_max
    feature_record["relative_deviation"] = feature_max / max(abs(feature_ref), 1e-15)
    feature_record["error_interval"] = [
        feature_ref - feature_max,
        feature_ref + feature_max,
    ]
    feature_record["amplification_ratio"] = feature_max / max(
        observed_ops.quantum, 1e-300
    )

    final_observed = observed.steps[-1]
    block_record = _deviation_record(
        level="block_accumulation",
        reference_value=final_observed.whole_objective,
        observed_value=final_observed.blocked_objective,
        quantum=observed_ops.quantum,
        details={
            "block_size": block_plan.block_size,
            "block_count": len(block_plan.blocks),
            "decision_sha256": block_plan.decision_sha256,
            "objective_gap": final_observed.objective_gap,
        },
    )

    reference_coefficients = np.asarray(
        reference.steps[-1].coefficients, dtype=np.float64
    )
    quantized_reference = observed_ops.quantize(reference_coefficients)
    coefficient_abs = np.abs(quantized_reference - reference_coefficients)
    coefficient_max = (
        float(np.max(coefficient_abs)) if coefficient_abs.size else 0.0
    )
    coefficient_index = (
        int(np.unravel_index(np.argmax(coefficient_abs), coefficient_abs.shape)[0])
        if coefficient_abs.size
        else -1
    )
    coefficient_reference = (
        float(reference_coefficients[coefficient_index])
        if coefficient_index >= 0
        else 0.0
    )
    quantization_record = _deviation_record(
        level="fixed_quantization",
        reference_value=coefficient_reference,
        observed_value=coefficient_reference + coefficient_max,
        quantum=observed_ops.quantum,
        details={
            "coefficient_index": coefficient_index,
            "max_abs_deviation": coefficient_max,
            "fixed_scale": observed_ops.fixed_scale,
            "rounding_mode": observed_ops.rounding_mode,
        },
    )
    quantization_record["observed_value"] = float(
        quantized_reference[coefficient_index]
    ) if coefficient_index >= 0 else 0.0
    quantization_record["absolute_deviation"] = coefficient_max
    quantization_record["relative_deviation"] = coefficient_max / max(
        abs(coefficient_reference), 1e-15
    )
    quantization_record["error_interval"] = [
        coefficient_reference - coefficient_max,
        coefficient_reference + coefficient_max,
    ]
    quantization_record["amplification_ratio"] = coefficient_max / max(
        observed_ops.quantum, 1e-300
    )

    observed_gradient, _ = compute_gradient(
        observed_matrix,
        labels,
        observed.coefficients,
        observed.intercept,
        observed.block_plan,
        observed_ops,
    )
    reference_gradient, _ = compute_gradient(
        reference_matrix,
        labels,
        reference.coefficients,
        reference.intercept,
        reference.block_plan,
        reference_ops,
    )
    gradient_abs = np.abs(observed_gradient - reference_gradient)
    gradient_max = float(np.max(gradient_abs)) if gradient_abs.size else 0.0
    gradient_index = (
        int(np.unravel_index(np.argmax(gradient_abs), gradient_abs.shape)[0])
        if gradient_abs.size
        else -1
    )
    gradient_reference = (
        float(reference_gradient[gradient_index]) if gradient_index >= 0 else 0.0
    )
    gradient_record = _deviation_record(
        level="gradient_update",
        reference_value=gradient_reference,
        observed_value=gradient_reference + gradient_max,
        quantum=observed_ops.quantum,
        details={
            "coefficient_index": gradient_index,
            "max_abs_deviation": gradient_max,
        },
    )
    gradient_record["observed_value"] = float(
        observed_gradient[gradient_index]
    ) if gradient_index >= 0 else 0.0
    gradient_record["absolute_deviation"] = gradient_max
    gradient_record["relative_deviation"] = gradient_max / max(
        abs(gradient_reference), 1e-15
    )
    gradient_record["error_interval"] = [
        gradient_reference - gradient_max,
        gradient_reference + gradient_max,
    ]
    gradient_record["amplification_ratio"] = gradient_max / max(
        observed_ops.quantum, 1e-300
    )

    levels = [
        feature_record,
        block_record,
        quantization_record,
        gradient_record,
    ]
    dominant = max(
        levels,
        key=lambda record: (
            record["amplification_ratio"],
            -LEVEL_ORDER.index(record["level"]),
        ),
    )["level"]
    path_spectrum = []
    for index, step in enumerate(observed.steps):
        reference_step = reference.steps[index]
        coefficient_error = float(
            np.max(
                np.abs(
                    np.asarray(step.coefficients)
                    - np.asarray(reference_step.coefficients)
                )
            )
            if step.coefficients
            else 0.0
        )
        path_spectrum.append(
            {
                "path_index": index,
                "strength": step.strength,
                "coefficient_max_abs_deviation": coefficient_error,
                "objective_abs_deviation": abs(
                    step.objective - reference_step.objective
                ),
                "objective_gap": step.objective_gap,
                "converged": step.converged,
                "reference_converged": reference_step.converged,
            }
        )
    input_fingerprint = sha256_json(
        {
            "matrix": observed_matrix.tolist(),
            "labels": labels.tolist(),
            "block_plans": [
                reference.block_plan.decision_sha256,
                observed.block_plan.decision_sha256,
            ],
            "precision": observed_ops.to_dict(),
            "strength_path": [step.strength for step in observed.steps],
        }
    )
    return {
        "reference": "float64_whole_block",
        "observed_precision": observed.precision,
        "precision_config": observed_ops.to_dict(),
        "block_decision_sha256": observed.block_plan.decision_sha256,
        "input_fingerprint": input_fingerprint,
        "levels": levels,
        "dominant_level": dominant,
        "path_spectrum": path_spectrum,
    }

