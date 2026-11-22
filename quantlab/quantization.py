from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import (
    BusinessError,
    E_QUANT_DTYPE,
    E_SCALE_INVALID,
    E_ZERO_POINT_RANGE,
)
from .serialization import (
    array_checksum,
    decode_array,
    encode_array,
)


ROUNDING_MODE = "rint_half_even"
QUANT_RANGES: dict[str, tuple[int, int]] = {
    "int8": (-128, 127),
    "uint8": (0, 255),
    "int4": (-8, 7),
    "uint4": (0, 15),
}


def quant_range(dtype: str, *, layer_id: str | None = None) -> tuple[int, int]:
    try:
        return QUANT_RANGES[dtype]
    except KeyError as exc:
        raise BusinessError(
            E_QUANT_DTYPE,
            f"未知量化类型: {dtype}",
            stage="quantization",
            layer_id=layer_id,
            details={"quant_dtype": dtype, "supported": sorted(QUANT_RANGES)},
        ) from exc


def _group_slices(size: int, group_size: int) -> list[slice]:
    return [slice(start, min(start + group_size, size)) for start in range(0, size, group_size)]


def _group_scales(values: np.ndarray, scale_hint: float, group_size: int) -> np.ndarray:
    flat = values.reshape(-1)
    global_max = float(np.max(np.abs(flat))) if flat.size else 0.0
    scales: list[float] = []
    for group in _group_slices(flat.size, group_size):
        group_values = flat[group]
        group_max = float(np.max(np.abs(group_values))) if group_values.size else 0.0
        ratio = group_max / global_max if global_max > 0.0 else 1.0
        # The manifest hint anchors the step size; the group term gives deterministic
        # per-group adaptation without any hidden calibration state.
        scale = float(scale_hint) * (0.75 + 0.25 * ratio)
        scales.append(max(scale, np.finfo(np.float64).tiny))
    return np.asarray(scales, dtype=np.float64)


def _expand_group_scales(
    scales: np.ndarray,
    *,
    shape: tuple[int, ...],
    group_size: int,
) -> np.ndarray:
    expanded = np.empty(int(np.prod(shape, dtype=np.int64)), dtype=np.float64)
    for group_index, group in enumerate(_group_slices(expanded.size, group_size)):
        expanded[group] = scales[group_index]
    return expanded.reshape(shape)


def _round_and_clip(values: np.ndarray, qmin: int, qmax: int) -> np.ndarray:
    return np.clip(np.rint(values), qmin, qmax).astype(np.int64)


def dequantize_codes(
    codes: np.ndarray,
    scales: np.ndarray,
    *,
    shape: tuple[int, ...],
    group_size: int,
    zero_point: int,
) -> np.ndarray:
    scale_grid = _expand_group_scales(scales, shape=shape, group_size=group_size)
    return np.ascontiguousarray((codes.astype(np.float64) - float(zero_point)) * scale_grid)


def recompute_fixed_point(
    weights: np.ndarray,
    scales: np.ndarray,
    *,
    group_size: int,
    zero_point: int,
    quant_dtype: str,
) -> tuple[np.ndarray, np.ndarray]:
    qmin, qmax = quant_range(quant_dtype)
    scale_grid = _expand_group_scales(scales, shape=weights.shape, group_size=group_size)
    codes = _round_and_clip(weights / scale_grid + float(zero_point), qmin, qmax)
    dequantized = dequantize_codes(
        codes,
        scales,
        shape=weights.shape,
        group_size=group_size,
        zero_point=zero_point,
    )
    return codes, dequantized


def _error_summary(values: np.ndarray, error: np.ndarray) -> dict[str, Any]:
    abs_error = np.abs(error)
    denominator = np.abs(values) + np.finfo(np.float64).eps
    signed_min = float(np.min(error)) if error.size else 0.0
    signed_max = float(np.max(error)) if error.size else 0.0
    return {
        "max_abs": float(np.max(abs_error)) if abs_error.size else 0.0,
        "mean_abs": float(np.mean(abs_error)) if abs_error.size else 0.0,
        "rmse": float(np.sqrt(np.mean(np.square(error)))) if error.size else 0.0,
        "mean_relative": float(np.mean(abs_error / denominator)) if error.size else 0.0,
        "max_relative": float(np.max(abs_error / denominator)) if error.size else 0.0,
        "interval": [min(signed_min, signed_max), max(signed_min, signed_max)],
    }


@dataclass
class QuantizedLayer:
    layer_id: str
    layer_index: int
    layer_shape: tuple[int, int]
    weight_revision: int
    quant_dtype: str
    scale_hint: float
    zero_point: int
    block_policy: dict[str, Any]
    group_size: int
    qmin: int
    qmax: int
    scales: np.ndarray
    codes: np.ndarray
    dequantized: np.ndarray
    original_checksum: str
    dequantized_checksum: str
    error: dict[str, Any]

    @property
    def weight_bytes(self) -> int:
        return int(self.dequantized.nbytes)

    def to_payload(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "layer_index": self.layer_index,
            "layer_shape": list(self.layer_shape),
            "weight_revision": self.weight_revision,
            "quant_dtype": self.quant_dtype,
            "scale_hint": self.scale_hint,
            "zero_point": self.zero_point,
            "block_policy": self.block_policy,
            "group_size": self.group_size,
            "qmin": self.qmin,
            "qmax": self.qmax,
            "rounding": ROUNDING_MODE,
            "scales": encode_array(self.scales),
            "codes": encode_array(self.codes),
            "dequantized": encode_array(self.dequantized),
            "original_checksum": self.original_checksum,
            "dequantized_checksum": self.dequantized_checksum,
            "weight_bytes": self.weight_bytes,
            "error": self.error,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "QuantizedLayer":
        return cls(
            layer_id=str(payload["layer_id"]),
            layer_index=int(payload["layer_index"]),
            layer_shape=tuple(int(x) for x in payload["layer_shape"]),
            weight_revision=int(payload["weight_revision"]),
            quant_dtype=str(payload["quant_dtype"]),
            scale_hint=float(payload["scale_hint"]),
            zero_point=int(payload["zero_point"]),
            block_policy=dict(payload["block_policy"]),
            group_size=int(payload["group_size"]),
            qmin=int(payload["qmin"]),
            qmax=int(payload["qmax"]),
            scales=decode_array(payload["scales"]),
            codes=decode_array(payload["codes"]),
            dequantized=decode_array(payload["dequantized"]),
            original_checksum=str(payload["original_checksum"]),
            dequantized_checksum=str(payload["dequantized_checksum"]),
            error=dict(payload["error"]),
        )


def quantize_layer(
    weights: np.ndarray,
    entry: dict[str, Any],
) -> QuantizedLayer:
    layer_id = str(entry["layer_id"])
    layer_index = int(entry["layer_index"])
    qmin, qmax = quant_range(str(entry["quant_dtype"]), layer_id=layer_id)
    try:
        scale_hint = float(entry["scale_hint"])
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            E_SCALE_INVALID,
            "scale_hint 必须为数值",
            stage="quantization",
            layer_id=layer_id,
            layer_index=layer_index,
            details={"scale_hint": entry.get("scale_hint")},
        ) from exc
    if not np.isfinite(scale_hint) or scale_hint <= 0.0:
        raise BusinessError(
            E_SCALE_INVALID,
            f"scale_hint 必须为正有限数: {scale_hint}",
            stage="quantization",
            layer_id=layer_id,
            layer_index=layer_index,
            details={"scale_hint": scale_hint},
        )
    zero_point = entry["zero_point"]
    if (
        isinstance(zero_point, bool)
        or not isinstance(zero_point, (int, np.integer))
        or int(zero_point) < qmin
        or int(zero_point) > qmax
    ):
        raise BusinessError(
            E_ZERO_POINT_RANGE,
            f"zero_point 越界: {zero_point} 不在 [{qmin}, {qmax}]",
            stage="quantization",
            layer_id=layer_id,
            layer_index=layer_index,
            details={"zero_point": zero_point, "range": [qmin, qmax]},
        )
    policy = entry.get("block_policy") or {}
    try:
        group_size = int(policy.get("group_size", 4))
    except (AttributeError, TypeError, ValueError) as exc:
        raise BusinessError(
            E_SCALE_INVALID,
            "block_policy.group_size 非法",
            stage="quantization",
            layer_id=layer_id,
            layer_index=layer_index,
            details={"block_policy": policy},
        ) from exc
    if group_size < 1:
        raise BusinessError(
            E_SCALE_INVALID,
            "block_policy.group_size 必须大于 0",
            stage="quantization",
            layer_id=layer_id,
            layer_index=layer_index,
            details={"group_size": group_size},
        )
    weights = np.ascontiguousarray(np.asarray(weights, dtype=np.float64))
    expected_shape = tuple(int(x) for x in entry["layer_shape"])
    if weights.shape != expected_shape:
        raise BusinessError(
            E_SCALE_INVALID,
            "权重数组形状与清单不一致",
            stage="quantization",
            layer_id=layer_id,
            layer_index=layer_index,
            details={"expected": list(expected_shape), "actual": list(weights.shape)},
        )
    scales = _group_scales(weights, scale_hint, group_size)
    codes, dequantized = recompute_fixed_point(
        weights,
        scales,
        group_size=group_size,
        zero_point=int(zero_point),
        quant_dtype=str(entry["quant_dtype"]),
    )
    error = weights - dequantized
    return QuantizedLayer(
        layer_id=layer_id,
        layer_index=layer_index,
        layer_shape=expected_shape,
        weight_revision=int(entry["weight_revision"]),
        quant_dtype=str(entry["quant_dtype"]),
        scale_hint=scale_hint,
        zero_point=int(zero_point),
        block_policy=dict(policy),
        group_size=group_size,
        qmin=qmin,
        qmax=qmax,
        scales=scales,
        codes=codes,
        dequantized=dequantized,
        original_checksum=array_checksum(weights),
        dequantized_checksum=array_checksum(dequantized),
        error=_error_summary(weights, error),
    )


def single_scale_dequantize(weights: np.ndarray, entry: dict[str, Any]) -> np.ndarray:
    """Diagnostic path: use one scale for all weight groups."""
    qmin, qmax = quant_range(str(entry["quant_dtype"]), layer_id=str(entry["layer_id"]))
    scale = float(entry["scale_hint"])
    zero_point = int(entry["zero_point"])
    codes = _round_and_clip(weights / scale + float(zero_point), qmin, qmax)
    return np.ascontiguousarray((codes.astype(np.float64) - zero_point) * scale)


def verify_dequantized_weights(
    record: QuantizedLayer,
    weights: np.ndarray,
) -> dict[str, Any]:
    recomputed_codes, recomputed_dequant = recompute_fixed_point(
        weights,
        record.scales,
        group_size=record.group_size,
        zero_point=record.zero_point,
        quant_dtype=record.quant_dtype,
    )
    from_codes = dequantize_codes(
        record.codes,
        record.scales,
        shape=record.layer_shape,
        group_size=record.group_size,
        zero_point=record.zero_point,
    )
    return {
        "layer_id": record.layer_id,
        "codes_match": bool(np.array_equal(recomputed_codes, record.codes)),
        "dequantized_from_codes_match": bool(np.array_equal(from_codes, record.dequantized)),
        "independent_recompute_match": bool(np.array_equal(recomputed_dequant, record.dequantized)),
        "max_abs_fixed_point_deviation": float(
            np.max(np.abs(recomputed_dequant - record.dequantized))
        )
        if record.dequantized.size
        else 0.0,
        "rounding": ROUNDING_MODE,
        "scale_count": int(record.scales.size),
        "group_size": record.group_size,
    }
