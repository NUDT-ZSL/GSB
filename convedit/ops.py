"""Deterministic float64 convolution and interpolation implementations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import OPERATOR_IMPLEMENTATION_VERSION, OPERATOR_VERSIONS, ALIGN_MODES
from .errors import BusinessError, E_PARAM_OUT_OF_RANGE, E_SHAPE_MISMATCH, E_NONFINITE_INPUT
from .graph import MAX_INTERPOLATION_SIZE, validate_node_params
from .util import sha256_json


CONV_LOOP_ORDER = [
    "batch",
    "output_channel",
    "output_row",
    "output_col",
    "input_channel",
    "kernel_row",
    "kernel_col",
]


@dataclass(frozen=True)
class OperatorResult:
    tensor: np.ndarray
    metadata: dict[str, Any]
    normalized_params: dict[str, Any]


def parameter_fingerprint(op: str, params: dict[str, Any]) -> str:
    return sha256_json(
        {
            "op": op,
            "params": params,
            "operator_version": OPERATOR_VERSIONS[op],
            "implementation_version": OPERATOR_IMPLEMENTATION_VERSION,
        }
    )


def cache_key(op: str, params: dict[str, Any], input_fingerprint: str) -> str:
    return sha256_json(
        {
            "parameter_fingerprint": parameter_fingerprint(op, params),
            "input_tensor_fingerprint": input_fingerprint,
            "operator_implementation_version": OPERATOR_VERSIONS[op],
        }
    )


def _ensure_finite_input(array: np.ndarray) -> None:
    if not np.isfinite(array).all():
        count = int(np.count_nonzero(~np.isfinite(array)))
        raise BusinessError(
            E_NONFINITE_INPUT,
            "input tensor contains non-finite elements",
            stage="compute",
            details={"nonfinite_count": count},
        )


def _require_input_shape(array: np.ndarray, in_channels: int) -> None:
    if array.ndim != 4 or array.shape[1] != in_channels:
        raise BusinessError(
            E_SHAPE_MISMATCH,
            "conv2d input must be NCHW with the declared in_channels",
            stage="compute",
            details={"input_shape": list(array.shape), "in_channels": in_channels},
        )


def conv2d(array: np.ndarray, params: dict[str, Any], *, padding_value: float) -> OperatorResult:
    normalized = validate_node_params("conv2d", params, node_id="<runtime>")
    x = np.asarray(array, dtype=np.float64)
    _ensure_finite_input(x)
    _require_input_shape(x, int(normalized["in_channels"]))
    kernel_h, kernel_w = (int(value) for value in normalized["kernel_size"])
    stride_h, stride_w = (int(value) for value in normalized["stride"])
    pad_h, pad_w = (int(value) for value in normalized["padding"])
    in_channels = int(normalized["in_channels"])
    out_channels = int(normalized["out_channels"])
    weights = np.asarray(normalized["weights"], dtype=np.float64)
    bias = np.asarray(normalized["bias"], dtype=np.float64)
    batch, channels, height, width = x.shape
    if channels != in_channels:
        raise BusinessError(
            E_SHAPE_MISMATCH,
            "conv2d input channel count mismatch",
            stage="compute",
            details={"input_channels": channels, "in_channels": in_channels},
        )
    out_h = (height + 2 * pad_h - kernel_h) // stride_h + 1
    out_w = (width + 2 * pad_w - kernel_w) // stride_w + 1
    if out_h < 1 or out_w < 1:
        raise BusinessError(
            E_SHAPE_MISMATCH,
            "conv2d output shape would be non-positive",
            stage="compute",
            details={
                "input_shape": list(x.shape),
                "kernel_size": [kernel_h, kernel_w],
                "stride": [stride_h, stride_w],
                "padding": [pad_h, pad_w],
            },
        )
    padded = np.full(
        (batch, channels, height + 2 * pad_h, width + 2 * pad_w),
        np.float64(padding_value),
        dtype=np.float64,
    )
    if pad_h or pad_w:
        padded[:, :, pad_h : pad_h + height, pad_w : pad_w + width] = x
    else:
        padded[:, :, :, :] = x
    output = np.zeros((batch, out_channels, out_h, out_w), dtype=np.float64)
    for n in range(batch):
        for oc in range(out_channels):
            for oh in range(out_h):
                for ow in range(out_w):
                    acc = np.float64(0.0)
                    for ic in range(in_channels):
                        for kh in range(kernel_h):
                            ih = oh * stride_h + kh
                            for kw in range(kernel_w):
                                iw = ow * stride_w + kw
                                acc = acc + padded[n, ic, ih, iw] * weights[oc, ic, kh, kw]
                    output[n, oc, oh, ow] = acc + bias[oc]
    metadata = {
        "op": "conv2d",
        "input_shape": list(x.shape),
        "output_shape": list(output.shape),
        "kernel_size": [kernel_h, kernel_w],
        "stride": [stride_h, stride_w],
        "padding": [pad_h, pad_w],
        "in_channels": in_channels,
        "out_channels": out_channels,
        "padding_value": float(padding_value),
        "compute_dtype": "float64",
        "loop_order": CONV_LOOP_ORDER,
        "bias_position": "after_input_channel_and_kernel_accumulation",
        "operator_version": OPERATOR_VERSIONS["conv2d"],
    }
    return OperatorResult(tensor=output, metadata=metadata, normalized_params=normalized)


def _axis_coordinates(in_size: int, out_size: int, align_mode: str) -> np.ndarray:
    if align_mode not in ALIGN_MODES:
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            "invalid interpolation align_mode",
            stage="parameter_construction",
            details={"align_mode": align_mode},
        )
    positions = np.arange(out_size, dtype=np.float64)
    if align_mode == "half_pixel":
        return (positions + np.float64(0.5)) * (np.float64(in_size) / np.float64(out_size)) - np.float64(0.5)
    if align_mode == "asymmetric":
        return positions * (np.float64(in_size) / np.float64(out_size))
    if out_size == 1:
        return np.zeros(out_size, dtype=np.float64)
    return positions * (np.float64(in_size - 1) / np.float64(out_size - 1))


def _resolve_output_size(
    array: np.ndarray, normalized: dict[str, Any]
) -> tuple[int, int, dict[str, Any]]:
    in_h, in_w = int(array.shape[2]), int(array.shape[3])
    requested: dict[str, Any]
    if "output_size" in normalized:
        out_h, out_w = (int(value) for value in normalized["output_size"])
        requested = {"output_size": [out_h, out_w]}
    else:
        scale_h, scale_w = (float(value) for value in normalized["scale_factor"])
        out_h = max(1, int(math.floor(in_h * scale_h)))
        out_w = max(1, int(math.floor(in_w * scale_w)))
        requested = {"scale_factor": [scale_h, scale_w]}
    if not 1 <= out_h <= MAX_INTERPOLATION_SIZE or not 1 <= out_w <= MAX_INTERPOLATION_SIZE:
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            "resolved interpolation output size is out of range",
            stage="parameter_construction",
            details={"output_size": [out_h, out_w], "maximum": MAX_INTERPOLATION_SIZE},
        )
    return out_h, out_w, requested


def bilinear(array: np.ndarray, params: dict[str, Any]) -> OperatorResult:
    normalized = validate_node_params("bilinear", params, node_id="<runtime>")
    x = np.asarray(array, dtype=np.float64)
    _ensure_finite_input(x)
    if x.ndim != 4:
        raise BusinessError(
            E_SHAPE_MISMATCH,
            "bilinear input must be four-dimensional NCHW",
            stage="compute",
            details={"input_shape": list(x.shape)},
        )
    batch, channels, in_h, in_w = x.shape
    out_h, out_w, requested = _resolve_output_size(x, normalized)
    align_mode = str(normalized["align_mode"])
    y_coords = _axis_coordinates(in_h, out_h, align_mode)
    x_coords = _axis_coordinates(in_w, out_w, align_mode)
    output = np.zeros((batch, channels, out_h, out_w), dtype=np.float64)
    for n in range(batch):
        for c in range(channels):
            for oh in range(out_h):
                y = y_coords[oh]
                y0_raw = int(math.floor(float(y)))
                y1_raw = y0_raw + 1
                y0 = min(max(y0_raw, 0), in_h - 1)
                y1 = min(max(y1_raw, 0), in_h - 1)
                wy = np.float64(y - np.float64(y0_raw))
                for ow in range(out_w):
                    x_coord = x_coords[ow]
                    x0_raw = int(math.floor(float(x_coord)))
                    x1_raw = x0_raw + 1
                    x0 = min(max(x0_raw, 0), in_w - 1)
                    x1 = min(max(x1_raw, 0), in_w - 1)
                    wx = np.float64(x_coord - np.float64(x0_raw))
                    v00 = x[n, c, y0, x0]
                    v01 = x[n, c, y0, x1]
                    v10 = x[n, c, y1, x0]
                    v11 = x[n, c, y1, x1]
                    top = v00 * (np.float64(1.0) - wx) + v01 * wx
                    bottom = v10 * (np.float64(1.0) - wx) + v11 * wx
                    output[n, c, oh, ow] = top * (np.float64(1.0) - wy) + bottom * wy
    metadata = {
        "op": "bilinear",
        "input_shape": list(x.shape),
        "output_shape": list(output.shape),
        **requested,
        "alignment": align_mode,
        "rounding_rule": "floor_ceil",
        "source_index_rule": "floor_and_ceil_with_unit_weights",
        "boundary": "clip",
        "boundary_clipped": True,
        "coordinate_scale": [in_h / out_h, in_w / out_w],
        "compute_dtype": "float64",
        "operator_version": OPERATOR_VERSIONS["bilinear"],
    }
    return OperatorResult(tensor=output, metadata=metadata, normalized_params=normalized)


def nearest(array: np.ndarray, params: dict[str, Any]) -> OperatorResult:
    normalized = validate_node_params("nearest", params, node_id="<runtime>")
    x = np.asarray(array, dtype=np.float64)
    _ensure_finite_input(x)
    if x.ndim != 4:
        raise BusinessError(
            E_SHAPE_MISMATCH,
            "nearest input must be four-dimensional NCHW",
            stage="compute",
            details={"input_shape": list(x.shape)},
        )
    batch, channels, in_h, in_w = x.shape
    out_h, out_w, requested = _resolve_output_size(x, normalized)
    align_mode = str(normalized["align_mode"])
    y_coords = _axis_coordinates(in_h, out_h, align_mode)
    x_coords = _axis_coordinates(in_w, out_w, align_mode)
    y_indices = np.clip(np.floor(y_coords).astype(np.int64), 0, in_h - 1)
    x_indices = np.clip(np.floor(x_coords).astype(np.int64), 0, in_w - 1)
    output = np.empty((batch, channels, out_h, out_w), dtype=np.float64)
    for n in range(batch):
        for c in range(channels):
            for oh in range(out_h):
                y = int(y_indices[oh])
                for ow in range(out_w):
                    output[n, c, oh, ow] = x[n, c, y, int(x_indices[ow])]
    metadata = {
        "op": "nearest",
        "input_shape": list(x.shape),
        "output_shape": list(output.shape),
        **requested,
        "alignment": align_mode,
        "rounding_rule": "floor",
        "source_index_rule": "floor",
        "boundary": "clip",
        "boundary_clipped": True,
        "coordinate_scale": [in_h / out_h, in_w / out_w],
        "compute_dtype": "float64",
        "operator_version": OPERATOR_VERSIONS["nearest"],
    }
    return OperatorResult(tensor=output, metadata=metadata, normalized_params=normalized)


def execute_operator(op: str, array: np.ndarray, params: dict[str, Any], *, padding_value: float) -> OperatorResult:
    if op == "conv2d":
        return conv2d(array, params, padding_value=padding_value)
    if op == "bilinear":
        return bilinear(array, params)
    if op == "nearest":
        return nearest(array, params)
    raise BusinessError(
        E_PARAM_OUT_OF_RANGE,
        f"unsupported operator {op!r}",
        stage="parameter_construction",
    )
