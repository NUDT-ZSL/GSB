"""Deterministic integer-domain quantized convolution and dequantization."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np

from .errors import AccOverflowError, QuantParamError
from .graph import QuantNode
from .packs import PackTensor
from .util import (
    array_digest,
    clamp_integer_array,
    dequantize_integer_array,
    finite_positive,
    quantize_float_array,
)


IMPLEMENTATION_VERSION = "integer-conv-qconv-v1"


@dataclass
class ActivationSource:
    kind: str
    integer: np.ndarray
    output: np.ndarray | None
    source_scale: float
    source_bit_width: int
    source_digest: str
    producer_id: str | None = None


@dataclass
class RuntimeNodeParams:
    weight_bit_width: int
    weight_scale: float
    activation_bit_width: int
    activation_scale: float
    accumulator_bits: int
    overflow_policy: str


@dataclass
class ConvResult:
    integer: np.ndarray
    output: np.ndarray
    integer_digest: str
    output_digest: str
    macs_executed: int
    macs_expected: int
    activation_clamped: int
    weight_clamped: int
    overflow_events: list[dict[str, Any]] = field(default_factory=list)
    error_summary: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0


def output_dimensions(node: QuantNode, input_shape: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    batch, _, height, width = input_shape
    out_h = (height + 2 * node.padding[0] - node.kernel[0]) // node.stride[0] + 1
    out_w = (width + 2 * node.padding[1] - node.kernel[1]) // node.stride[1] + 1
    return batch, node.out_channels, out_h, out_w


def expected_macs(node: QuantNode, input_shape: tuple[int, int, int, int]) -> int:
    return int(math.prod(output_dimensions(node, input_shape))) * node.in_channels * math.prod(node.kernel)


def runtime_params(node: QuantNode) -> RuntimeNodeParams:
    return RuntimeNodeParams(
        weight_bit_width=node.weight_bit_width,
        weight_scale=node.weight_scale,
        activation_bit_width=node.activation_bit_width,
        activation_scale=node.activation_scale,
        accumulator_bits=node.accumulator_bits,
        overflow_policy=node.overflow_policy,
    )


def validate_runtime_params(node_id: str, params: RuntimeNodeParams) -> None:
    for label, bits in (
        ("weight_bit_width", params.weight_bit_width),
        ("activation_bit_width", params.activation_bit_width),
    ):
        if not isinstance(bits, int) or bits < 2 or bits > 16:
            raise QuantParamError(
                f"node {node_id}: {label} must be in [2, 16], got {bits!r}",
                node_id=node_id,
                details={"field": label, "value": bits},
            )
    for label, scale in (
        ("weight_scale", params.weight_scale),
        ("activation_scale", params.activation_scale),
    ):
        if not finite_positive(scale):
            raise QuantParamError(
                f"node {node_id}: {label} must be finite and positive, got {scale!r}",
                node_id=node_id,
                details={"field": label, "value": str(scale)},
            )
    if params.accumulator_bits < 2 or params.accumulator_bits > 64:
        raise QuantParamError(
            f"node {node_id}: accumulator_bits must be in [2, 64], got {params.accumulator_bits}",
            node_id=node_id,
            details={"field": "accumulator_bits", "value": params.accumulator_bits},
        )
    if params.overflow_policy not in {"error", "saturate"}:
        raise QuantParamError(
            f"node {node_id}: overflow_policy must be error or saturate",
            node_id=node_id,
        )


def prepare_activation(
    node: QuantNode,
    source: ActivationSource,
    params: RuntimeNodeParams,
    quantization_enabled: bool,
    rounding_mode: str,
) -> tuple[np.ndarray, int]:
    source_values = np.asarray(source.integer, dtype=np.int64)
    if source_values.ndim != 4:
        raise QuantParamError(
            f"node {node.id}: activation must be rank 4, got shape {source_values.shape}",
            node_id=node.id,
        )
    if source_values.shape[1] != node.in_channels:
        raise QuantParamError(
            f"node {node.id}: activation has {source_values.shape[1]} channels, expected {node.in_channels}",
            node_id=node.id,
        )
    if source.kind == "root":
        # Stored input integers are the source of truth.  Scaling is applied by
        # dequantization; a bit-width edit is realized by fixed-width clamping.
        return clamp_integer_array(source_values, params.activation_bit_width)
    if not quantization_enabled:
        return clamp_integer_array(source_values, params.activation_bit_width)
    if source.output is None:
        raise QuantParamError(
            f"node {node.id}: upstream activation output is unavailable", node_id=node.id
        )
    return quantize_float_array(
        source.output,
        bit_width=params.activation_bit_width,
        scale=params.activation_scale,
        rounding_mode=rounding_mode,
    )


def prepare_weights(
    node: QuantNode,
    weight_pack: PackTensor,
    params: RuntimeNodeParams,
    quantization_enabled: bool,
    rounding_mode: str,
) -> tuple[np.ndarray, int]:
    values = np.asarray(weight_pack.values, dtype=np.int64)
    if tuple(values.shape) != tuple(node.weight_shape):
        raise QuantParamError(
            f"node {node.id}: weight shape {values.shape} does not match {node.weight_shape}",
            node_id=node.id,
        )
    # Weight integers are stored fixed-point values.  A bit-width edit clamps
    # them to the new signed range; scale edits affect theta (dequantization)
    # without silently rebuilding the integer payload.
    return clamp_integer_array(values, params.weight_bit_width)


def integer_convolution(
    node: QuantNode,
    activation: np.ndarray,
    weights: np.ndarray,
    accumulator_bits: int,
    overflow_policy: str,
    max_overflow_events: int = 16,
) -> tuple[np.ndarray, int, list[dict[str, Any]]]:
    """Integer convolution with fixed-width overflow checks.

    Loop order is fixed as output channel, output row, output column, kernel
    row, kernel column, input channel.  The accumulator is checked before every
    addition, so a failed run reports the exact multiplication/accumulation
    position that crossed the configured signed accumulator range.
    """

    act = np.asarray(activation, dtype=np.int64)
    ker = np.asarray(weights, dtype=np.int64)
    if act.ndim != 4 or ker.ndim != 4:
        raise QuantParamError(f"node {node.id}: convolution inputs must be rank 4", node_id=node.id)
    batch, in_channels, height, width = act.shape
    out_channels, kernel_in_channels, kernel_h, kernel_w = ker.shape
    if in_channels != node.in_channels or kernel_in_channels != node.in_channels:
        raise QuantParamError(f"node {node.id}: input/weight channel mismatch", node_id=node.id)
    if (kernel_h, kernel_w) != node.kernel or out_channels != node.out_channels:
        raise QuantParamError(f"node {node.id}: weight geometry does not match node", node_id=node.id)
    stride_h, stride_w = node.stride
    pad_h, pad_w = node.padding
    out_h = (height + 2 * pad_h - kernel_h) // stride_h + 1
    out_w = (width + 2 * pad_w - kernel_w) // stride_w + 1
    if out_h <= 0 or out_w <= 0:
        raise QuantParamError(f"node {node.id}: non-positive output shape", node_id=node.id)

    lower = -(1 << (accumulator_bits - 1))
    upper = (1 << (accumulator_bits - 1)) - 1
    result = np.zeros((batch, out_channels, out_h, out_w), dtype=np.int64)
    overflow_events: list[dict[str, Any]] = []
    macs_executed = 0

    for batch_index in range(batch):
        for output_channel in range(out_channels):
            for output_row in range(out_h):
                for output_col in range(out_w):
                    accumulator = 0
                    for kernel_row in range(kernel_h):
                        input_row = output_row * stride_h - pad_h + kernel_row
                        for kernel_col in range(kernel_w):
                            input_col = output_col * stride_w - pad_w + kernel_col
                            for input_channel in range(in_channels):
                                macs_executed += 1
                                if (
                                    input_row < 0
                                    or input_row >= height
                                    or input_col < 0
                                    or input_col >= width
                                ):
                                    product = 0
                                else:
                                    product = int(act[batch_index, input_channel, input_row, input_col]) * int(
                                        ker[output_channel, input_channel, kernel_row, kernel_col]
                                    )
                                candidate = accumulator + product
                                if candidate < lower or candidate > upper:
                                    position = {
                                        "batch": batch_index,
                                        "output_channel": output_channel,
                                        "output_row": output_row,
                                        "output_col": output_col,
                                        "kernel_row": kernel_row,
                                        "kernel_col": kernel_col,
                                        "input_channel": input_channel,
                                        "mac_index": macs_executed - 1,
                                    }
                                    if overflow_policy == "error":
                                        raise AccOverflowError(
                                            f"node {node.id}: accumulator overflow at {position}",
                                            node_id=node.id,
                                            accumulator_bits=accumulator_bits,
                                            position=position,
                                            accumulator_before=accumulator,
                                            product=product,
                                        )
                                    if len(overflow_events) < max_overflow_events:
                                        overflow_events.append(
                                            {
                                                **position,
                                                "accumulator_before": accumulator,
                                                "product": product,
                                                "clamped": lower if candidate < lower else upper,
                                            }
                                        )
                                    accumulator = lower if candidate < lower else upper
                                else:
                                    accumulator = candidate
                    result[batch_index, output_channel, output_row, output_col] = accumulator
    return result, macs_executed, overflow_events


def dequantize_result(
    integer: np.ndarray,
    params: RuntimeNodeParams,
    quantization_enabled: bool,
    rounding_mode: str,
    dequant_decimals: int,
) -> tuple[np.ndarray, dict[str, float], dict[str, Any]]:
    if quantization_enabled:
        weight_scale = params.weight_scale
        activation_scale = params.activation_scale
    else:
        weight_scale = 1.0
        activation_scale = 1.0
    output, error_summary = dequantize_integer_array(
        integer,
        weight_scale=weight_scale,
        activation_scale=activation_scale,
        rounding_mode=rounding_mode,
        dequant_decimals=dequant_decimals,
    )
    factor = Decimal(str(weight_scale)) * Decimal(str(activation_scale))
    metadata = {
        "rounding_mode": rounding_mode,
        "dequant_decimals": dequant_decimals,
        "dequant_factor": str(factor),
        "quantization_enabled": quantization_enabled,
    }
    return output, error_summary, metadata


def compute_node(
    node: QuantNode,
    source: ActivationSource,
    weight_pack: PackTensor,
    params: RuntimeNodeParams,
    *,
    quantization_enabled: bool,
    rounding_mode: str,
    dequant_decimals: int,
    debug_node_delay_ms: int = 0,
) -> ConvResult:
    started = time.perf_counter()
    validate_runtime_params(node.id, params)
    activation, activation_clamped = prepare_activation(
        node, source, params, quantization_enabled, rounding_mode
    )
    weights, weight_clamped = prepare_weights(
        node, weight_pack, params, quantization_enabled, rounding_mode
    )
    if debug_node_delay_ms > 0:
        time.sleep(debug_node_delay_ms / 1000.0)
    integer, macs_executed, overflow_events = integer_convolution(
        node,
        activation,
        weights,
        params.accumulator_bits,
        params.overflow_policy,
    )
    output, error_summary, dequant_metadata = dequantize_result(
        integer,
        params,
        quantization_enabled,
        rounding_mode,
        dequant_decimals,
    )
    expected = int(np.prod(integer.shape)) * node.in_channels * math.prod(node.kernel)
    error_summary = dict(error_summary)
    error_summary.update(
        {
            "integer_min": int(integer.min()) if integer.size else 0,
            "integer_max": int(integer.max()) if integer.size else 0,
            "integer_mean_abs": float(np.mean(np.abs(integer))) if integer.size else 0.0,
            "output_mean_abs": float(np.mean(np.abs(output))) if output.size else 0.0,
            "snapshot_only_overflow_events": len(overflow_events),
        }
    )
    metadata = {
        **dequant_metadata,
        "weight_bit_width": params.weight_bit_width,
        "activation_bit_width": params.activation_bit_width,
        "weight_scale": params.weight_scale,
        "activation_scale": params.activation_scale,
        "accumulator_bits": params.accumulator_bits,
        "overflow_policy": params.overflow_policy,
        "implementation_version": IMPLEMENTATION_VERSION,
        "activation_requant_clamped": activation_clamped,
        "weight_requant_clamped": weight_clamped,
    }
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return ConvResult(
        integer=integer,
        output=output,
        integer_digest=array_digest(integer),
        output_digest=array_digest(output),
        macs_executed=macs_executed,
        macs_expected=expected,
        activation_clamped=activation_clamped,
        weight_clamped=weight_clamped,
        overflow_events=overflow_events,
        error_summary=error_summary,
        metadata=metadata,
        elapsed_ms=elapsed_ms,
    )
