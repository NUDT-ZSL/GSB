from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .config import EngineConfig
from .errors import E_TOLERANCE_EXCEEDED, BusinessError
from .inference import activate
from .manifest import LayerSpec
from .quant import dequantize_layer
from .util import array_digest, float_list, json_digest
from .weights import generate_weight


DOMINANT_SOURCES = ("quantization_rounding", "group_scaling", "activation_accumulation")


def _error_metrics(
    actual: np.ndarray,
    reference: np.ndarray,
    previous_mean_abs: float | None,
) -> dict[str, Any]:
    delta = np.asarray(actual, dtype=np.float64) - np.asarray(
        reference, dtype=np.float64
    )
    absolute = np.abs(delta)
    p05, p50, p95 = np.quantile(absolute, [0.05, 0.50, 0.95]).tolist()
    numerator = float(np.linalg.norm(delta))
    denominator = float(np.linalg.norm(reference))
    relative = 0.0 if denominator == 0.0 and numerator == 0.0 else numerator / max(
        denominator, 1e-300
    )
    mean_abs = float(np.mean(absolute))
    if previous_mean_abs is None:
        amplification = 1.0
    elif previous_mean_abs == 0.0:
        amplification = 1.0 if mean_abs == 0.0 else None
    else:
        amplification = mean_abs / previous_mean_abs
    return {
        "abs_mean": mean_abs,
        "abs_max": float(np.max(absolute)),
        "abs_p05": float(p05),
        "abs_p50": float(p50),
        "abs_p95": float(p95),
        "relative": relative,
        "error_range": [float(np.min(absolute)), float(np.max(absolute))],
        "error_interval_90": [float(p05), float(p95)],
        "amplification_ratio": amplification,
    }


def compute_error_spectrum(
    manifest_layers: tuple[LayerSpec, ...],
    quant_layers: Mapping[str, dict[str, Any]],
    initial_input: np.ndarray,
    config: EngineConfig,
    *,
    committed_activations: Mapping[str, list[float]] | None = None,
) -> dict[str, Any]:
    reference_input = np.asarray(initial_input, dtype=np.float64)
    rounding_input = np.asarray(initial_input, dtype=np.float64)
    scaling_input = np.asarray(initial_input, dtype=np.float64)
    operational_input = np.asarray(initial_input, dtype=np.float64)
    previous_means: dict[str, float | None] = {name: None for name in DOMINANT_SOURCES}
    previous_serial_mean: float | None = None
    rows: list[dict[str, Any]] = []
    reference_prediction: np.ndarray | None = None
    snapshot_reference_prediction: np.ndarray | None = None
    operational_prediction: np.ndarray | None = None
    for layer in manifest_layers:
        quant_layer = quant_layers[layer.layer_id]
        full_weight = generate_weight(layer)
        rounding_weight = dequantize_layer(quant_layer, scale_mode="exact")
        operational_weight = np.asarray(
            quant_layer["dequant_weights"], dtype=np.float64
        ).reshape(layer.layer_shape)
        reference_activation = activate(full_weight, reference_input, "float64")
        rounding_activation = activate(rounding_weight, rounding_input, "float64")
        scaling_activation = activate(operational_weight, scaling_input, "float64")
        operational_activation = activate(
            operational_weight, operational_input, config.accumulation_dtype
        )
        if committed_activations is not None:
            committed = np.asarray(
                committed_activations[layer.layer_id], dtype=np.float64
            )
            if array_digest(committed) != array_digest(operational_activation):
                raise BusinessError(
                    "E_INCONSISTENT",
                    "diagnostic replay does not match committed activation",
                    stage="accuracy_diagnostics",
                    layer_id=layer.layer_id,
                    layer_index=layer.layer_index,
                    details={"category": "activation_replay_mismatch"},
                )
        source_errors = {
            "quantization_rounding": _error_metrics(
                rounding_activation, reference_activation, previous_means["quantization_rounding"]
            ),
            "group_scaling": _error_metrics(
                scaling_activation, rounding_activation, previous_means["group_scaling"]
            ),
            "activation_accumulation": _error_metrics(
                operational_activation,
                scaling_activation,
                previous_means["activation_accumulation"],
            ),
        }
        serial = _error_metrics(
            operational_activation, reference_activation, previous_serial_mean
        )
        for source in DOMINANT_SOURCES:
            previous_means[source] = source_errors[source]["abs_mean"]
        previous_serial_mean = serial["abs_mean"]
        dominant = max(
            DOMINANT_SOURCES,
            key=lambda source: (source_errors[source]["abs_mean"], -DOMINANT_SOURCES.index(source)),
        )
        rows.append(
            {
                "layer_id": layer.layer_id,
                "layer_index": layer.layer_index,
                "weight_revision": layer.weight_revision,
                "sources": source_errors,
                "layer_serial": serial,
                "dominant_source": dominant,
                "reference_activation_digest": array_digest(reference_activation),
                "operational_activation_digest": array_digest(operational_activation),
                "prediction_preview": float_list(operational_activation[:8]),
            }
        )
        reference_input = reference_activation
        rounding_input = rounding_activation
        scaling_input = scaling_activation
        operational_input = operational_activation
        reference_prediction = reference_activation
        snapshot_reference_prediction = scaling_activation
        operational_prediction = operational_activation
    assert (
        reference_prediction is not None
        and snapshot_reference_prediction is not None
        and operational_prediction is not None
    )
    source_totals = {
        source: float(
            np.mean([row["sources"][source]["abs_mean"] for row in rows])
        )
        for source in DOMINANT_SOURCES
    }
    overall_dominant = max(
        DOMINANT_SOURCES,
        key=lambda source: (source_totals[source], -DOMINANT_SOURCES.index(source)),
    )
    violations: list[dict[str, Any]] = []
    for row in rows:
        serial = row["layer_serial"]
        if (
            serial["abs_mean"] > config.tolerance_abs
            or serial["relative"] > config.tolerance_rel
        ):
            violations.append(
                {
                    "layer_id": row["layer_id"],
                    "layer_index": row["layer_index"],
                    "abs_mean": serial["abs_mean"],
                    "abs_max": serial["abs_max"],
                    "relative": serial["relative"],
                    "tolerance_abs": config.tolerance_abs,
                    "tolerance_rel": config.tolerance_rel,
                }
            )
    final_serial = _error_metrics(
        operational_prediction, reference_prediction, previous_serial_mean
    )
    final_activation_accumulation = _error_metrics(
        operational_prediction,
        snapshot_reference_prediction,
        previous_means["activation_accumulation"],
    )
    diagnostics: dict[str, Any] = {
        "reference": {
            "description": (
                "float64 inference with original full-precision generated weights; "
                "external quantization reference"
            ),
            "reference_prediction": float_list(reference_prediction),
            "reference_prediction_digest": array_digest(reference_prediction),
        },
        "snapshot_reference": {
            "description": (
                "float64 inference with committed stored-scale dequantized weights; "
                "in-snapshot activation-accumulation reference"
            ),
            "prediction": float_list(snapshot_reference_prediction),
            "prediction_digest": array_digest(snapshot_reference_prediction),
        },
        "operational": {
            "accumulation_dtype": config.accumulation_dtype,
            "prediction": float_list(operational_prediction),
            "prediction_digest": array_digest(operational_prediction),
        },
        "layers": rows,
        "source_means": source_totals,
        "overall_dominant_source": overall_dominant,
        "final_serial_error": final_serial,
        "final_activation_accumulation_error": final_activation_accumulation,
        "tolerances": {
            "abs_mean": config.tolerance_abs,
            "relative": config.tolerance_rel,
        },
        "violations": violations,
        "passed": not violations,
        "checksum": "",
    }
    payload = {key: value for key, value in diagnostics.items() if key != "checksum"}
    diagnostics["checksum"] = json_digest(payload)
    if violations:
        first = violations[0]
        raise BusinessError(
            E_TOLERANCE_EXCEEDED,
            "layer serial error exceeds configured tolerance",
            stage="accuracy_diagnostics",
            layer_id=first["layer_id"],
            layer_index=first["layer_index"],
            details={
                "violations": violations,
                "tolerance_abs": config.tolerance_abs,
                "tolerance_rel": config.tolerance_rel,
            },
            trace=["diagnostics:tolerance_guard"],
        )
    return diagnostics
