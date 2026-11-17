"""NumPy float32 and fixed-point inference with layer-wise diagnostics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np

from .assets import Assets, LayerParameters, Sample
from .batching import BatchPlan
from .canonical import digest_float32_sequence, float32_bits
from .config import BatchConfig
from .errors import WorkbenchError


INT64_MAX = (1 << 63) - 1
ACTIVATION_INT_LIMIT = 1 << 62
RELATIVE_FLOOR = 1e-12
BOUND_METHOD = {
    "rounding": "round_half_away_from_zero",
    "quantization": "q(v)=round_half_away(S*v)",
    "accumulation": "A=sum(q(x)*q(w))+S*q(b), saturating at signed int64",
    "requantization": "y=round_half_away(A/S), saturating at 2^62 in integer grid",
    "local_bound_grid": "ceil(max_j(0.5*sum_i|q(w_j,i)|+0.5*sum_i|q(x_i)|+n/4)/S+1)",
    "cumulative_bound_grid": "ceil(max_j(sum_i|q(w_j,i)|)/S*previous_cumulative+local_bound)",
    "derivation": (
        "Each q(x)=S*x+e_x and q(w)=S*w+e_w with |e|<=0.5. "
        "Expanding the dot product gives the first two cross terms plus n/4; "
        "+1 grid covers 0.5 bias rounding and 0.5 output requantization. "
        "ReLU is 1-Lipschitz; the max absolute row sum is used as the infinity-norm gain."
    ),
}


@dataclass(frozen=True)
class FixedLayer:
    name: str
    q_weight: np.ndarray
    q_bias: np.ndarray
    gain: float
    weight_abs_row_max: int


def _round_away_array(values: np.ndarray, scale: int) -> np.ndarray:
    scaled = np.asarray(values, dtype=np.float64) * float(scale)
    if not np.all(np.isfinite(scaled)):
        raise WorkbenchError("E_CONFIG", "量化缩放后出现非有限值", {"quant_scale": scale})
    maximum = float(np.max(np.abs(scaled), initial=0.0))
    if maximum > float(INT64_MAX - 2):
        raise WorkbenchError(
            "E_CONFIG",
            "量化缩放因子过大，整数表示将超出 int64 安全范围",
            {"quant_scale": scale, "max_scaled": maximum},
        )
    magnitude = np.floor(np.abs(scaled) + 0.5)
    rounded = np.sign(scaled) * magnitude
    return np.asarray(rounded, dtype=np.int64)


def prepare_fixed_model(assets: Assets, config: BatchConfig) -> tuple[FixedLayer, ...]:
    scale = config.quant_scale
    prepared: list[FixedLayer] = []
    for layer in assets.layers:
        q_weight = _round_away_array(layer.weight, scale)
        q_bias = _round_away_array(layer.bias, scale)
        abs_rows = np.sum(np.abs(q_weight), axis=1, dtype=np.int64)
        row_max = int(np.max(abs_rows, initial=0))
        prepared.append(
            FixedLayer(
                name=layer.name,
                q_weight=q_weight,
                q_bias=q_bias,
                gain=float(row_max) / float(scale),
                weight_abs_row_max=row_max,
            )
        )
    return tuple(prepared)


def float_forward(sample: Sample, layers: Sequence[LayerParameters]) -> dict[str, Any]:
    """Per-sample NumPy float32 forward pass.

    Keeping the per-sample call path fixed makes diagnostics independent of batch
    size and position. NumPy sums are used instead of BLAS GEMM to avoid
    batch-shape-dependent reduction kernels.
    """

    activation = np.asarray(sample.values, dtype=np.float32)
    trace: dict[str, np.ndarray] = {}
    for index, layer in enumerate(layers):
        preactivation = np.empty(layer.bias.size, dtype=np.float32)
        for row_index in range(layer.bias.size):
            dot = np.sum(
                np.asarray(layer.weight[row_index], dtype=np.float32) * activation,
                dtype=np.float32,
            )
            preactivation[row_index] = np.float32(dot + layer.bias[row_index])
        if index < len(layers) - 1:
            activation = np.maximum(preactivation, np.float32(0.0))
        else:
            activation = preactivation
        trace[layer.name] = activation.copy()
    return {"trace": trace, "output": trace[layers[-1].name]}


def _safe_dot_rows(q_weight: np.ndarray, q_activation: np.ndarray) -> tuple[np.ndarray, int]:
    fan_in = int(q_activation.size)
    max_weight = int(np.max(np.abs(q_weight), initial=0))
    max_activation = int(np.max(np.abs(q_activation), initial=0))
    safe_bound = max_weight * max_activation * max(1, fan_in)
    if safe_bound <= INT64_MAX:
        products = q_weight * q_activation.reshape(1, -1)
        return np.sum(products, axis=1, dtype=np.int64), 0

    products = q_weight.astype(object) * q_activation.astype(object).reshape(1, -1)
    totals = np.sum(products, axis=1, dtype=object)
    clipped: list[int] = []
    saturated = 0
    for value in totals:
        integer = int(value)
        if integer > INT64_MAX:
            integer = INT64_MAX
            saturated += 1
        elif integer < -INT64_MAX - 1:
            integer = -INT64_MAX - 1
            saturated += 1
        clipped.append(integer)
    return np.asarray(clipped, dtype=np.int64), saturated


def _add_bias_saturating(
    accumulated: np.ndarray, q_bias: np.ndarray, scale: int
) -> tuple[np.ndarray, int]:
    values: list[int] = []
    saturated = 0
    for index, value in enumerate(accumulated.tolist()):
        total = int(value) + int(q_bias[index]) * int(scale)
        if total > INT64_MAX:
            total = INT64_MAX
            saturated += 1
        elif total < -INT64_MAX - 1:
            total = -INT64_MAX - 1
            saturated += 1
        values.append(total)
    return np.asarray(values, dtype=np.int64), saturated


def _requantize_saturating(accumulated: np.ndarray, scale: int) -> tuple[np.ndarray, int]:
    values: list[int] = []
    saturated = 0
    for value in accumulated.tolist():
        integer = int(value)
        sign = -1 if integer < 0 else 1
        quotient, remainder = divmod(abs(integer), int(scale))
        if 2 * remainder >= int(scale):
            quotient += 1
        quantized = sign * quotient
        if quantized > ACTIVATION_INT_LIMIT:
            quantized = ACTIVATION_INT_LIMIT
            saturated += 1
        elif quantized < -ACTIVATION_INT_LIMIT:
            quantized = -ACTIVATION_INT_LIMIT
            saturated += 1
        values.append(quantized)
    return np.asarray(values, dtype=np.int64), saturated


def _dequantize_float32(quantized: np.ndarray, scale: int) -> np.ndarray:
    return (quantized.astype(np.float64) / float(scale)).astype(np.float32)


def _layer_error_bound(
    fixed_layer: FixedLayer,
    q_activation: np.ndarray,
    scale: int,
    previous_cumulative: int,
) -> tuple[int, int, dict[str, Any]]:
    fan_in = int(q_activation.size)
    activation_abs_sum = int(np.sum(np.abs(q_activation), dtype=np.int64))
    weighted_cross = 0.5 * float(fixed_layer.weight_abs_row_max)
    activation_cross = 0.5 * float(activation_abs_sum)
    cross_terms = 0.25 * float(fan_in)
    dot_bound = (weighted_cross + activation_cross + cross_terms) / float(scale)
    local_bound = int(math.ceil(dot_bound + 1.0))
    propagated = fixed_layer.gain * float(previous_cumulative)
    cumulative = int(math.ceil(propagated + float(local_bound)))
    inputs = {
        "fan_in": fan_in,
        "scale": int(scale),
        "weight_abs_row_max": int(fixed_layer.weight_abs_row_max),
        "activation_abs_sum": activation_abs_sum,
        "weight_cross_term_grid": weighted_cross,
        "activation_cross_term_grid": activation_cross,
        "cross_product_term_grid": cross_terms,
        "raw_dot_bound_grid": dot_bound,
        "bias_rounding_grid": 0.5,
        "requant_rounding_grid": 0.5,
        "propagation_gain": fixed_layer.gain,
        "previous_cumulative_grid": int(previous_cumulative),
    }
    return local_bound, cumulative, inputs


def fixed_forward(
    sample: Sample,
    fixed_layers: Sequence[FixedLayer],
    scale: int,
) -> dict[str, Any]:
    q_activation = _round_away_array(sample.values, scale)
    trace: dict[str, np.ndarray] = {}
    layer_records: dict[str, dict[str, Any]] = {}
    cumulative_bound = 0
    any_saturation = False

    for index, fixed_layer in enumerate(fixed_layers):
        dot_acc, dot_saturated = _safe_dot_rows(fixed_layer.q_weight, q_activation)
        accumulated, bias_saturated = _add_bias_saturating(dot_acc, fixed_layer.q_bias, scale)
        quantized, requant_saturated = _requantize_saturating(accumulated, scale)
        if index < len(fixed_layers) - 1:
            quantized = np.maximum(quantized, 0)
        saturation_count = dot_saturated + bias_saturated + requant_saturated
        any_saturation = any_saturation or saturation_count > 0

        local_bound, cumulative_bound, bound_inputs = _layer_error_bound(
            fixed_layer, q_activation, scale, cumulative_bound
        )
        trace[fixed_layer.name] = _dequantize_float32(quantized, scale)
        layer_records[fixed_layer.name] = {
            "local_error_bound_grid": local_bound,
            "cumulative_error_bound_grid": cumulative_bound,
            "accumulation_saturation_count": saturation_count,
            "bounds_valid": saturation_count == 0,
            "bound_inputs": bound_inputs,
        }
        q_activation = quantized

    return {
        "trace": trace,
        "output": trace[fixed_layers[-1].name],
        "q_output": q_activation,
        "layer_records": layer_records,
        "any_saturation": any_saturation,
    }


def _max_abs(values: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    return float(np.max(np.abs(values)))


def _attribution_chain(
    float_trace: dict[str, np.ndarray],
    fixed_trace: dict[str, np.ndarray],
    layer_records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    layer_names = list(float_trace)
    rows: list[dict[str, Any]] = []
    previous_relative = 0.0
    for layer_name in layer_names:
        float_values = float_trace[layer_name]
        fixed_values = fixed_trace[layer_name]
        absolute_error = _max_abs(fixed_values - float_values)
        float_abs = _max_abs(float_values)
        fixed_abs = _max_abs(fixed_values)
        relative_error = absolute_error / max(float_abs, RELATIVE_FLOOR)
        delta = relative_error - previous_relative
        record = layer_records[layer_name]
        rows.append(
            {
                "layer": layer_name,
                "float_max_abs": float_abs,
                "fixed_max_abs": fixed_abs,
                "abs_error": absolute_error,
                "relative_error": relative_error,
                "previous_relative_error": previous_relative,
                "relative_delta": delta,
                "increase": max(0.0, delta),
                "local_error_bound_grid": int(record["local_error_bound_grid"]),
                "cumulative_error_bound_grid": int(record["cumulative_error_bound_grid"]),
                "accumulation_saturation_count": int(record["accumulation_saturation_count"]),
                "bounds_valid": bool(record["bounds_valid"]),
                "bound_inputs": record["bound_inputs"],
            }
        )
        previous_relative = relative_error

    increases = [row["increase"] for row in rows]
    total_increase = math.fsum(increases)
    if total_increase <= 0.0 and any(row["abs_error"] > 0.0 for row in rows):
        increases = [row["abs_error"] for row in rows]
        total_increase = math.fsum(increases)
    cumulative_share = 0.0
    dominant_index = 0
    dominant_share = 0.0
    for index, row in enumerate(rows):
        share = increases[index] / total_increase if total_increase > 0.0 else 0.0
        cumulative_share += share
        row["increase_share"] = share
        row["cumulative_share"] = cumulative_share
        if share > dominant_share:
            dominant_share = share
            dominant_index = index
    output_relative = rows[-1]["relative_error"] if rows else 0.0
    max_relative_layer = None
    max_relative = 0.0
    if rows:
        max_index = max(range(len(rows)), key=lambda index: rows[index]["relative_error"])
        max_relative_layer = rows[max_index]["layer"]
        max_relative = rows[max_index]["relative_error"]
    return {
        "layers": rows,
        "output_relative_deviation": output_relative,
        "max_relative_error_layer": max_relative_layer,
        "max_relative_error": max_relative,
        "dominant_layer": rows[dominant_index]["layer"] if rows and total_increase > 0 else None,
        "dominant_error_share": dominant_share if total_increase > 0 else 0.0,
        "dominant_cumulative_share": (
            rows[dominant_index]["cumulative_share"] if rows and total_increase > 0 else 0.0
        ),
    }


def _empty_float_diagnostics(layer_names: Sequence[str]) -> dict[str, Any]:
    rows = [
        {
            "layer": name,
            "float_max_abs": 0.0,
            "fixed_max_abs": 0.0,
            "abs_error": 0.0,
            "relative_error": 0.0,
            "previous_relative_error": 0.0,
            "relative_delta": 0.0,
            "increase": 0.0,
            "increase_share": 0.0,
            "cumulative_share": 0.0,
            "local_error_bound_grid": 0,
            "cumulative_error_bound_grid": 0,
            "accumulation_saturation_count": 0,
            "bounds_valid": True,
            "bound_inputs": {},
        }
        for name in layer_names
    ]
    return {
        "applicable": False,
        "method": "float32 reference path; no fixed-point quantization is applied",
        "layers": rows,
        "output_relative_deviation": 0.0,
        "max_relative_error_layer": None,
        "max_relative_error": 0.0,
        "dominant_layer": None,
        "dominant_error_share": 0.0,
        "dominant_cumulative_share": 0.0,
        "drift_first_exceedance": None,
    }


def infer_sample(
    assets: Assets,
    sample: Sample,
    config: BatchConfig,
    fixed_layers: Sequence[FixedLayer] | None,
) -> dict[str, Any]:
    reference = float_forward(sample, assets.layers)
    reference_output = reference["output"]

    if config.precision_mode == "float32":
        output = reference_output
        diagnostics = _empty_float_diagnostics(assets.layer_names)
        q_output: list[int] | None = None
        any_saturation = False
    else:
        if fixed_layers is None:
            raise WorkbenchError("E_CONFIG", "定点推理缺少已准备模型")
        fixed = fixed_forward(sample, fixed_layers, config.quant_scale)
        output = fixed["output"]
        chain = _attribution_chain(reference["trace"], fixed["trace"], fixed["layer_records"])
        first_drift: dict[str, Any] | None = None
        for row in chain["layers"]:
            if row["relative_error"] > config.drift_threshold:
                first_drift = {
                    "sample_id": sample.sample_id,
                    "layer": row["layer"],
                    "relative_error": row["relative_error"],
                    "threshold": config.drift_threshold,
                }
                break
        diagnostics = {
            "applicable": True,
            "method": "fixed-point integer inference compared with float32 reference",
            "layers": chain["layers"],
            "output_relative_deviation": chain["output_relative_deviation"],
            "max_relative_error_layer": chain["max_relative_error_layer"],
            "max_relative_error": chain["max_relative_error"],
            "dominant_layer": chain["dominant_layer"],
            "dominant_error_share": chain["dominant_error_share"],
            "dominant_cumulative_share": chain["dominant_cumulative_share"],
            "drift_first_exceedance": first_drift,
        }
        q_output = [int(value) for value in fixed["q_output"]]
        any_saturation = bool(fixed["any_saturation"])

    return {
        "sample_id": sample.sample_id,
        "output": [float(value) for value in output],
        "reference_output": [float(value) for value in reference_output],
        "output_bits": [float32_bits(value) for value in output.astype(np.float32)],
        "q_output": q_output,
        "any_saturation": any_saturation,
        "diagnostics": diagnostics,
    }


def run_batch(
    assets: Assets,
    config: BatchConfig,
    plan: BatchPlan,
    fixed_layers: Sequence[FixedLayer] | None = None,
) -> dict[str, Any]:
    sample_lookup = assets.sample_by_id()
    samples = [infer_sample(assets, sample_lookup[sample_id], config, fixed_layers) for sample_id in plan.sample_ids]
    output_parts = [(sample["sample_id"], np.asarray(sample["output"], dtype=np.float32)) for sample in samples]
    output_digest = digest_float32_sequence(output_parts)
    first_drift: dict[str, Any] | None = None
    for sample in samples:
        candidate = sample["diagnostics"].get("drift_first_exceedance")
        if candidate is not None:
            first_drift = candidate
            break
    max_output_deviation = max(
        (float(sample["diagnostics"].get("output_relative_deviation", 0.0)) for sample in samples),
        default=0.0,
    )
    max_bound = max(
        (
            int(layer["cumulative_error_bound_grid"])
            for sample in samples
            for layer in sample["diagnostics"].get("layers", [])
        ),
        default=0,
    )
    drift = first_drift is not None
    return {
        "batch_index": plan.index,
        "batch_id": plan.batch_id,
        "sample_ids": list(plan.sample_ids),
        "sample_indices": list(plan.sample_indices),
        "precision_mode": config.precision_mode,
        "quant_scale": config.quant_scale,
        "config_version": config.config_version,
        "status": "complete",
        "stale": False,
        "outputs": [sample["output"] for sample in samples],
        "output_summary_sha256": output_digest,
        "max_output_relative_deviation": max_output_deviation,
        "max_cumulative_error_bound_grid": max_bound,
        "drift": drift,
        "business_codes": ["E_DRIFT"] if drift else [],
        "first_drift": first_drift,
        "samples": samples,
    }


def output_bytes(sample: dict[str, Any]) -> bytes:
    return np.asarray(sample["output"], dtype=np.float32).tobytes(order="C")
