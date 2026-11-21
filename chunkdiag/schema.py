"""Task parsing and parameter normalization.

The public task format is intentionally JSON-friendly. Shapes are documented as
NHWC for activations and OIHW for convolution weights.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from .errors import (
    DiagnosticError,
    E_DTYPE_UNSUPPORTED,
    E_OP_UNSUPPORTED,
    E_PARAM_INVALID,
    E_SHAPE_INVALID,
)


STAGE_VALIDATION = "parameter_validation"
STAGE_PARTITION = "block_partition"
STAGE_NUMERIC = "numeric_compute"
STAGE_COMPARE = "deviation_compare"

SUPPORTED_OPS = {"conv2d", "interpolate", "nearest", "bilinear", "bicubic"}
SUPPORTED_INTERP_MODES = {"nearest", "bilinear", "bicubic"}
SUPPORTED_DTYPES = {"float32", "float64"}


@dataclass(frozen=True)
class Tolerance:
    atol: float = 1e-12
    rtol: float = 1e-9

    def as_dict(self) -> dict[str, float]:
        return {"atol": self.atol, "rtol": self.rtol}


@dataclass(frozen=True)
class ResourceLimits:
    max_resident_bytes: int = 256 * 1024 * 1024
    max_blocks: int = 1_000_000
    max_live_intermediates: int = 3
    allow_smaller_blocks: bool = True
    allow_dtype_downgrade: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_resident_bytes": self.max_resident_bytes,
            "max_blocks": self.max_blocks,
            "max_live_intermediates": self.max_live_intermediates,
            "allow_smaller_blocks": self.allow_smaller_blocks,
            "allow_dtype_downgrade": self.allow_dtype_downgrade,
        }


@dataclass(frozen=True)
class NormalizedJob:
    job_id: str
    op_kind: str
    family: str
    interp_mode: str | None
    input_shape: tuple[int, int, int, int]
    kernel_shape: tuple[int, int, int, int] | None
    output_shape: tuple[int, int, int, int]
    stride: tuple[int, int]
    padding: tuple[int, int, int, int]
    padding_spec: Any
    scale: tuple[float, float] | None
    seed: int
    weight_seed: int | None
    dtype: str
    block_policy: dict[str, Any]
    tolerance: Tolerance
    tolerance_raw: dict[str, Any]
    resource_limits: ResourceLimits
    injection: dict[str, str]
    max_violation_records: int
    spectrum_detail_limit: int
    raw_task: Mapping[str, Any] = field(repr=False)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value: Any, *, job_id: str, field_name: str) -> int:
    if not _is_int(value) or value <= 0:
        raise DiagnosticError(
            E_SHAPE_INVALID,
            STAGE_VALIDATION,
            job_id,
            field_name,
            f"{field_name} must contain positive integers",
            {"value": value},
        )
    return int(value)


def _parse_shape(value: Any, *, job_id: str, field_name: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise DiagnosticError(
            E_SHAPE_INVALID,
            STAGE_VALIDATION,
            job_id,
            field_name,
            f"{field_name} must be a JSON array",
            {"value": value},
        )
    return tuple(
        _positive_int(item, job_id=job_id, field_name=f"{field_name}[{index}]")
        for index, item in enumerate(value)
    )


def _parse_pair(
    value: Any,
    *,
    job_id: str,
    field_name: str,
    default: tuple[int, int],
    positive: bool = True,
) -> tuple[int, int]:
    if value is None:
        return default
    if _is_int(value):
        pair = (int(value), int(value))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        if not all(_is_int(item) for item in value):
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                field_name,
                f"{field_name} must be an integer or a pair of integers",
                {"value": value},
            )
        pair = (int(value[0]), int(value[1]))
    else:
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            field_name,
            f"{field_name} must be an integer or a pair of integers",
            {"value": value},
        )
    if positive and (pair[0] <= 0 or pair[1] <= 0):
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            field_name,
            f"{field_name} must be positive",
            {"value": list(pair)},
        )
    return pair


def _parse_padding(
    value: Any,
    *,
    job_id: str,
    height: int,
    width: int,
    stride: tuple[int, int],
    kernel: tuple[int, int] | None,
) -> tuple[tuple[int, int, int, int], Any]:
    if value is None:
        value = "valid"

    if isinstance(value, str):
        mode = value.lower()
        if mode == "valid":
            return (0, 0, 0, 0), value
        if mode == "same" and kernel is not None:
            kh, kw = kernel
            out_h = math.ceil(height / stride[0])
            out_w = math.ceil(width / stride[1])
            pad_h = max((out_h - 1) * stride[0] + kh - height, 0)
            pad_w = max((out_w - 1) * stride[1] + kw - width, 0)
            top, bottom = pad_h // 2, pad_h - pad_h // 2
            left, right = pad_w // 2, pad_w - pad_w // 2
            return (top, bottom, left, right), value
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            "padding",
            "padding must be 'valid', 'same' for convolution, an integer, or explicit pads",
            {"value": value},
        )

    if _is_int(value):
        pad = int(value)
        pads = (pad, pad, pad, pad)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        if all(_is_int(item) for item in value):
            pads = (int(value[0]), int(value[0]), int(value[1]), int(value[1]))
        else:
            try:
                pads = (
                    int(value[0][0]),
                    int(value[0][1]),
                    int(value[1][0]),
                    int(value[1][1]),
                )
            except (TypeError, IndexError, ValueError) as exc:
                raise DiagnosticError(
                    E_PARAM_INVALID,
                    STAGE_VALIDATION,
                    job_id,
                    "padding",
                    "padding pair must be [pad_h, pad_w] or [[top, bottom], [left, right]]",
                    {"value": value},
                ) from exc
    elif isinstance(value, (list, tuple)) and len(value) == 4:
        if not all(_is_int(item) for item in value):
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                "padding",
                "four-sided padding must contain integers",
                {"value": value},
            )
        pads = tuple(int(item) for item in value)  # type: ignore[assignment]
    else:
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            "padding",
            "unsupported padding declaration",
            {"value": value},
        )

    if any(item < 0 for item in pads):
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            "padding",
            "padding values must be non-negative",
            {"value": list(pads)},
        )

    if kernel is not None:
        max_top_bottom = kernel[0] - 1
        max_left_right = kernel[1] - 1
        if pads[0] > max_top_bottom or pads[1] > max_top_bottom:
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                "padding",
                "vertical padding exceeds the declared range",
                {
                    "padding": list(pads),
                    "declared_max_top_bottom": max_top_bottom,
                },
            )
        if pads[2] > max_left_right or pads[3] > max_left_right:
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                "padding",
                "horizontal padding exceeds the declared range",
                {
                    "padding": list(pads),
                    "declared_max_left_right": max_left_right,
                },
            )
    else:
        max_top_bottom = height - 1
        max_left_right = width - 1
        if pads[0] > max_top_bottom or pads[1] > max_top_bottom:
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                "padding",
                "vertical interpolation padding exceeds the declared range",
                {
                    "padding": list(pads),
                    "declared_max_top_bottom": max_top_bottom,
                },
            )
        if pads[2] > max_left_right or pads[3] > max_left_right:
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                "padding",
                "horizontal interpolation padding exceeds the declared range",
                {
                    "padding": list(pads),
                    "declared_max_left_right": max_left_right,
                },
            )
    return pads, value


def _parse_block_policy(value: Any, *, job_id: str) -> dict[str, Any]:
    if value is None:
        return {
            "block_size": [32, 32],
            "candidates": [[32, 32]],
            "selection": "largest_fitting",
        }
    if _is_int(value) or (isinstance(value, (list, tuple)) and len(value) == 2):
        if isinstance(value, (list, tuple)) and not all(_is_int(item) for item in value):
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                "block_policy.block_size",
                "block size must contain two integers",
                {"value": value},
            )
        return {
            "block_size": value,
            "candidates": [value],
            "selection": "fixed",
        }
    if not isinstance(value, Mapping):
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            "block_policy",
            "block_policy must be an object, an integer, or a pair",
            {"value": value},
        )

    policy = dict(value)
    block_size = policy.get("block_size", policy.get("block", [32, 32]))
    candidates = policy.get("candidates")
    if candidates is None:
        candidates = [block_size]
    if not isinstance(candidates, (list, tuple)) or not candidates:
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            "block_policy.candidates",
            "block candidates must be a non-empty array",
            {"value": candidates},
        )
    selection = str(policy.get("selection", "fixed")).lower()
    if selection not in {"fixed", "largest_fitting"}:
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            "block_policy.selection",
            "block selection must be 'fixed' or 'largest_fitting'",
            {"value": policy.get("selection")},
        )
    policy["block_size"] = block_size
    policy["candidates"] = list(candidates)
    policy["selection"] = selection
    return policy


def _parse_tolerance_raw(value: Any) -> dict[str, Any]:
    if value is None:
        return {"atol": 1e-12, "rtol": 1e-9}
    if not isinstance(value, Mapping):
        return {"atol": value, "rtol": 1e-9}
    return {
        "atol": value.get("atol", 1e-12),
        "rtol": value.get("rtol", 1e-9),
    }


def _positive_int_limit(
    value: Any, *, job_id: str, field_name: str, default: int
) -> int:
    if value is None:
        return default
    if not _is_int(value) or value <= 0:
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            field_name,
            f"{field_name} must be a positive integer",
            {"value": value},
        )
    return int(value)


def _positive_int_config(
    value: Any, *, job_id: str, field_name: str, default: int
) -> int:
    if value is None:
        return default
    if not _is_int(value) or value <= 0:
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            field_name,
            f"{field_name} must be a positive integer",
            {"value": value},
        )
    return int(value)


def parse_resource_limits(value: Any, *, job_id: str) -> ResourceLimits:
    if value is None:
        return ResourceLimits()
    if not isinstance(value, Mapping):
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            "resource_limits",
            "resource_limits must be an object",
            {"value": value},
        )
    return ResourceLimits(
        max_resident_bytes=_positive_int_limit(
            value.get("max_resident_bytes"),
            job_id=job_id,
            field_name="resource_limits.max_resident_bytes",
            default=256 * 1024 * 1024,
        ),
        max_blocks=_positive_int_limit(
            value.get("max_blocks"),
            job_id=job_id,
            field_name="resource_limits.max_blocks",
            default=1_000_000,
        ),
        max_live_intermediates=_positive_int_limit(
            value.get("max_live_intermediates"),
            job_id=job_id,
            field_name="resource_limits.max_live_intermediates",
            default=3,
        ),
        allow_smaller_blocks=bool(value.get("allow_smaller_blocks", True)),
        allow_dtype_downgrade=bool(value.get("allow_dtype_downgrade", True)),
    )


def _parse_injection(value: Any, *, job_id: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            "injection",
            "injection must be an object keyed by stage",
            {"value": value},
        )
    known = {
        STAGE_VALIDATION: {"nonfinite_weight", "padding_overflow"},
        STAGE_PARTITION: {"zero_block_size"},
        STAGE_NUMERIC: {"nonfinite_output"},
        STAGE_COMPARE: {"nonpositive_tolerance"},
    }
    parsed: dict[str, str] = {}
    for stage, trigger in value.items():
        if stage not in known:
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                "injection",
                f"unknown injection stage: {stage}",
                {"stage": stage, "allowed_stages": sorted(known)},
            )
        if trigger not in known[stage]:
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                f"injection.{stage}",
                f"unknown injection trigger for {stage}: {trigger}",
                {"trigger": trigger, "allowed_triggers": sorted(known[stage])},
            )
        parsed[str(stage)] = str(trigger)
    return parsed


def normalize_job(
    task: Mapping[str, Any],
    *,
    input_array: np.ndarray | None = None,
    weight_array: np.ndarray | None = None,
) -> NormalizedJob:
    """Validate and normalize a task without running numerical work."""

    if not isinstance(task, Mapping):
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            "<unknown>",
            "task",
            "task must be an object",
        )

    job_id = task.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            str(job_id or "<missing>"),
            "job_id",
            "job_id must be a non-empty string",
            {"value": job_id},
        )

    op_kind = task.get("op_kind")
    if not isinstance(op_kind, str) or op_kind not in SUPPORTED_OPS:
        raise DiagnosticError(
            E_OP_UNSUPPORTED,
            STAGE_VALIDATION,
            job_id,
            "op_kind",
            f"unsupported op_kind: {op_kind!r}",
            {"value": op_kind, "supported": sorted(SUPPORTED_OPS)},
        )

    dtype = str(task.get("dtype", "float64")).lower()
    if dtype not in SUPPORTED_DTYPES:
        raise DiagnosticError(
            E_DTYPE_UNSUPPORTED,
            STAGE_VALIDATION,
            job_id,
            "dtype",
            f"unsupported dtype: {dtype!r}",
            {"value": dtype, "supported": sorted(SUPPORTED_DTYPES)},
        )

    shape_raw = _parse_shape(
        task.get("input_shape"), job_id=job_id, field_name="input_shape"
    )
    if len(shape_raw) != 4:
        raise DiagnosticError(
            E_SHAPE_INVALID,
            STAGE_VALIDATION,
            job_id,
            "input_shape",
            "input_shape must be four-dimensional [N, H, W, C]",
            {"value": list(shape_raw), "expected_rank": 4},
        )
    input_shape = (shape_raw[0], shape_raw[1], shape_raw[2], shape_raw[3])

    if input_array is not None:
        if tuple(int(item) for item in input_array.shape) != input_shape:
            raise DiagnosticError(
                E_SHAPE_INVALID,
                STAGE_VALIDATION,
                job_id,
                "input",
                "provided input array does not match input_shape",
                {
                    "input_shape": list(input_shape),
                    "array_shape": list(input_array.shape),
                },
            )

    stride = _parse_pair(
        task.get("stride"),
        job_id=job_id,
        field_name="stride",
        default=(1, 1),
    )

    family: str
    interp_mode: str | None
    kernel_shape: tuple[int, int, int, int] | None = None
    kernel_hw: tuple[int, int] | None = None
    if op_kind == "conv2d":
        family = "conv"
        interp_mode = None
        kernel_raw = _parse_shape(
            task.get("kernel_shape"), job_id=job_id, field_name="kernel_shape"
        )
        if len(kernel_raw) != 4:
            raise DiagnosticError(
                E_SHAPE_INVALID,
                STAGE_VALIDATION,
                job_id,
                "kernel_shape",
                "kernel_shape must be four-dimensional [O, C, KH, KW]",
                {"value": list(kernel_raw), "expected_rank": 4},
            )
        kernel_shape = (
            kernel_raw[0],
            kernel_raw[1],
            kernel_raw[2],
            kernel_raw[3],
        )
        if kernel_shape[1] != input_shape[3]:
            raise DiagnosticError(
                E_SHAPE_INVALID,
                STAGE_VALIDATION,
                job_id,
                "kernel_shape[1]",
                "kernel input channels must match input_shape[-1]",
                {
                    "kernel_input_channels": kernel_shape[1],
                    "input_channels": input_shape[3],
                },
            )
        kernel_hw = (kernel_shape[2], kernel_shape[3])
        if weight_array is not None and tuple(weight_array.shape) != kernel_shape:
            raise DiagnosticError(
                E_SHAPE_INVALID,
                STAGE_VALIDATION,
                job_id,
                "weight",
                "provided weight array does not match kernel_shape [O, C, KH, KW]",
                {
                    "kernel_shape": list(kernel_shape),
                    "array_shape": list(weight_array.shape),
                },
            )
    else:
        family = "interpolate"
        mode = task.get("interp_mode", op_kind if op_kind != "interpolate" else None)
        if mode not in SUPPORTED_INTERP_MODES:
            raise DiagnosticError(
                E_OP_UNSUPPORTED,
                STAGE_VALIDATION,
                job_id,
                "interp_mode",
                f"unsupported interp_mode: {mode!r}",
                {"value": mode, "supported": sorted(SUPPORTED_INTERP_MODES)},
            )
        interp_mode = str(mode)
        if weight_array is not None:
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                "weight",
                "interpolation tasks do not accept convolution weights",
            )

    padding, padding_spec = _parse_padding(
        task.get("padding"),
        job_id=job_id,
        height=input_shape[1],
        width=input_shape[2],
        stride=stride,
        kernel=kernel_hw,
    )

    scale: tuple[float, float] | None = None
    if family == "conv":
        padded_h = input_shape[1] + padding[0] + padding[1]
        padded_w = input_shape[2] + padding[2] + padding[3]
        out_h = (padded_h - kernel_shape[2]) // stride[0] + 1
        out_w = (padded_w - kernel_shape[3]) // stride[1] + 1
        if out_h <= 0 or out_w <= 0:
            raise DiagnosticError(
                E_SHAPE_INVALID,
                STAGE_VALIDATION,
                job_id,
                "input_shape",
                "convolution output shape is non-positive",
                {
                    "input_shape": list(input_shape),
                    "kernel_shape": list(kernel_shape or ()),
                    "stride": list(stride),
                    "padding": list(padding),
                },
            )
        output_shape = (input_shape[0], kernel_shape[0], out_h, out_w)
    else:
        scale_raw = task.get("scale")
        if scale_raw is None:
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                "scale",
                "interpolation tasks require scale",
            )
        if isinstance(scale_raw, (int, float)) and not isinstance(scale_raw, bool):
            scale_pair = (float(scale_raw), float(scale_raw))
        elif isinstance(scale_raw, (list, tuple)) and len(scale_raw) == 2:
            try:
                scale_pair = (float(scale_raw[0]), float(scale_raw[1]))
            except (TypeError, ValueError) as exc:
                raise DiagnosticError(
                    E_PARAM_INVALID,
                    STAGE_VALIDATION,
                    job_id,
                    "scale",
                    "scale must be numeric",
                    {"value": scale_raw},
                ) from exc
        else:
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                "scale",
                "scale must be a number or [scale_h, scale_w]",
                {"value": scale_raw},
            )
        if not all(math.isfinite(item) and item > 0 for item in scale_pair):
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                "scale",
                "scale values must be finite and positive",
                {"value": list(scale_pair)},
            )
        scale = scale_pair
        padded_h = input_shape[1] + padding[0] + padding[1]
        padded_w = input_shape[2] + padding[2] + padding[3]
        out_h = int(math.floor(padded_h * scale[0]))
        out_w = int(math.floor(padded_w * scale[1]))
        if out_h <= 0 or out_w <= 0:
            raise DiagnosticError(
                E_SHAPE_INVALID,
                STAGE_VALIDATION,
                job_id,
                "scale",
                "interpolation output shape is non-positive",
                {
                    "padded_shape": [padded_h, padded_w],
                    "scale": list(scale),
                },
            )
        output_shape = (input_shape[0], out_h, out_w, input_shape[3])

    seed = task.get("seed", 0)
    if not _is_int(seed) or seed < 0:
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            "seed",
            "seed must be a non-negative integer",
            {"value": seed},
        )
    weight_seed = task.get("weight_seed")
    if family == "conv":
        if weight_seed is None:
            weight_seed = seed + 1
        if not _is_int(weight_seed) or weight_seed < 0:
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                job_id,
                "weight_seed",
                "weight_seed must be a non-negative integer",
                {"value": weight_seed},
            )
    elif weight_seed is not None and (not _is_int(weight_seed) or weight_seed < 0):
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_VALIDATION,
            job_id,
            "weight_seed",
            "weight_seed must be a non-negative integer when provided",
            {"value": weight_seed},
        )

    tolerance_raw = _parse_tolerance_raw(task.get("tolerance"))
    try:
        tolerance = Tolerance(
            atol=float(tolerance_raw["atol"]),
            rtol=float(tolerance_raw["rtol"]),
        )
    except (TypeError, ValueError):
        tolerance = Tolerance(float("nan"), float("nan"))

    return NormalizedJob(
        job_id=job_id,
        op_kind=str(op_kind),
        family=family,
        interp_mode=interp_mode,
        input_shape=input_shape,
        kernel_shape=kernel_shape,
        output_shape=output_shape,
        stride=stride,
        padding=padding,
        padding_spec=padding_spec,
        scale=scale,
        seed=int(seed),
        weight_seed=int(weight_seed) if weight_seed is not None else None,
        dtype=dtype,
        block_policy=_parse_block_policy(task.get("block_policy"), job_id=job_id),
        tolerance=tolerance,
        tolerance_raw=tolerance_raw,
        resource_limits=parse_resource_limits(
            task.get("resource_limits"), job_id=job_id
        ),
        injection=_parse_injection(task.get("injection"), job_id=job_id),
        max_violation_records=_positive_int_config(
            task.get("max_violation_records"),
            job_id=job_id,
            field_name="max_violation_records",
            default=128,
        ),
        spectrum_detail_limit=_positive_int_config(
            task.get("spectrum_detail_limit"),
            job_id=job_id,
            field_name="spectrum_detail_limit",
            default=200_000,
        ),
        raw_task=task,
    )


def validate_tolerance(
    tolerance: Tolerance, *, job_id: str
) -> Tolerance:
    values = (tolerance.atol, tolerance.rtol)
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_COMPARE,
            job_id,
            "tolerance",
            "atol and rtol must be finite and strictly positive",
            {"atol": tolerance.atol, "rtol": tolerance.rtol},
        )
    return tolerance
