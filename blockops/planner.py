"""Validation and explicit block-plan construction."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import (
    E_AXIS_INVALID,
    E_BLOCK_SIZE_INVALID,
    E_DTYPE_UNSUPPORTED,
    E_EPS_INVALID,
    E_INPUT_NONFINITE,
    E_NORM_MODE_INVALID,
    E_OP_UNKNOWN,
    E_RESOURCE_LIMIT,
    E_SHAPE_INVALID,
    STAGE_BLOCK_STATS,
    STAGE_INPUT,
    Diagnostic,
    degradation,
)
from .models import ExecutionOptions, OperatorTask
from .util import sha256_json


SUPPORTED_DTYPES = {"float32", "float64"}
SOFTMAX_NORM_MODES = {"stable", "standard", "softmax", "row"}
LAYERNORM_NORM_MODES = {
    "population",
    "biased",
    "layer_norm",
    "layernorm",
    "last_dim",
    "axis",
    "row",
    "mean_var",
    "unbiased",
}


@dataclass(frozen=True)
class ValidationContext:
    axis: int
    axis_length: int
    outer_count: int
    eps: float
    norm_mode: str
    tensor_dtype: str


@dataclass(frozen=True)
class BlockPlan:
    axis: int
    axis_length: int
    outer_count: int
    requested_block_size: int | None
    effective_block_size: int
    block_ranges: tuple[tuple[int, int], ...]
    stats_dtype: str
    stats_memory_bytes: int
    requested_stats_memory_bytes: int
    max_block_elements_seen: int

    @property
    def block_count(self) -> int:
        return len(self.block_ranges)

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "axis_length": self.axis_length,
            "outer_count": self.outer_count,
            "requested_block_size": self.requested_block_size,
            "effective_block_size": self.effective_block_size,
            "block_count": self.block_count,
            "block_ranges": [[start, end] for start, end in self.block_ranges],
            "stats_dtype": self.stats_dtype,
            "stats_memory_estimate_bytes": self.stats_memory_bytes,
            "requested_stats_memory_estimate_bytes": self.requested_stats_memory_bytes,
            "max_block_elements_seen": self.max_block_elements_seen,
            "boundary_rule": "explicit half-open axis element ranges",
        }


@dataclass
class PlannedExecution:
    context: ValidationContext
    plan: BlockPlan | None
    diagnostics: list[Diagnostic]
    degradations: list[dict[str, Any]]
    hard_error: bool


def _diag(
    code: str,
    stage: str,
    task: OperatorTask,
    params: dict[str, Any],
    field: str | None,
    message: str,
    details: dict[str, Any] | None = None,
) -> Diagnostic:
    return Diagnostic(
        code=code,
        stage=stage,
        job_id=task.job_id,
        field=field,
        message=message,
        params=params,
        details=details or {},
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, (int, np.integer)) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(
        value, bool
    )


def plan_execution(
    task: OperatorTask,
    tensor: np.ndarray,
    options: ExecutionOptions,
) -> PlannedExecution:
    params = task.parameter_summary()
    params["faults"] = options.faults.to_dict()
    diagnostics: list[Diagnostic] = []
    degradations: list[dict[str, Any]] = []

    op_kind = task.op_kind
    if not isinstance(op_kind, str) or op_kind not in {"softmax", "layernorm"}:
        diagnostics.append(
            _diag(
                E_OP_UNKNOWN,
                STAGE_INPUT,
                task,
                params,
                "op_kind",
                "op_kind must be one of: softmax, layernorm",
                {"received": op_kind},
            )
        )
        return PlannedExecution(None, None, diagnostics, degradations, True)  # type: ignore[arg-type]

    dtype = task.dtype
    if not isinstance(dtype, str) or dtype not in SUPPORTED_DTYPES:
        diagnostics.append(
            _diag(
                E_DTYPE_UNSUPPORTED,
                STAGE_INPUT,
                task,
                params,
                "dtype",
                "only float32 and float64 are supported",
                {"received": dtype},
            )
        )
        return PlannedExecution(None, None, diagnostics, degradations, True)  # type: ignore[arg-type]
    if tensor.dtype.name != dtype:
        diagnostics.append(
            _diag(
                E_DTYPE_UNSUPPORTED,
                STAGE_INPUT,
                task,
                params,
                "dtype",
                "tensor dtype does not match task dtype",
                {"task_dtype": dtype, "tensor_dtype": tensor.dtype.name},
            )
        )
        return PlannedExecution(None, None, diagnostics, degradations, True)  # type: ignore[arg-type]

    norm_mode = task.norm_mode
    if op_kind == "softmax":
        if norm_mode is None:
            norm_mode = "stable"
        elif norm_mode not in SOFTMAX_NORM_MODES:
            diagnostics.append(
                _diag(
                    E_NORM_MODE_INVALID,
                    STAGE_INPUT,
                    task,
                    params,
                    "norm_mode",
                    "softmax norm_mode must be stable or standard",
                    {"received": norm_mode},
                )
            )
            return PlannedExecution(None, None, diagnostics, degradations, True)  # type: ignore[arg-type]
    else:
        if norm_mode is None:
            norm_mode = "population"
        elif norm_mode not in LAYERNORM_NORM_MODES:
            diagnostics.append(
                _diag(
                    E_NORM_MODE_INVALID,
                    STAGE_INPUT,
                    task,
                    params,
                    "norm_mode",
                    "layernorm norm_mode must be a population/biased alias or unbiased",
                    {"received": norm_mode},
                )
            )
            return PlannedExecution(None, None, diagnostics, degradations, True)  # type: ignore[arg-type]

    raw_shape = task.input_shape
    shape_valid = (
        isinstance(raw_shape, (list, tuple))
        and len(raw_shape) > 0
        and all(_is_int(item) and int(item) > 0 for item in raw_shape)
    )
    if not shape_valid:
        diagnostics.append(
            _diag(
                E_SHAPE_INVALID,
                STAGE_INPUT,
                task,
                params,
                "input_shape",
                "input_shape must be a non-empty list of positive integers",
                {"received": raw_shape, "tensor_shape": list(tensor.shape)},
            )
        )
        return PlannedExecution(None, None, diagnostics, degradations, True)  # type: ignore[arg-type]
    shape = tuple(int(item) for item in raw_shape)
    if shape != tensor.shape:
        diagnostics.append(
            _diag(
                E_SHAPE_INVALID,
                STAGE_INPUT,
                task,
                params,
                "input_shape",
                "task input_shape does not match tensor shape",
                {"task_shape": list(shape), "tensor_shape": list(tensor.shape)},
            )
        )
        return PlannedExecution(None, None, diagnostics, degradations, True)  # type: ignore[arg-type]

    axis = task.axis
    if not _is_int(axis) or int(axis) < -len(shape) or int(axis) >= len(shape):
        diagnostics.append(
            _diag(
                E_AXIS_INVALID,
                STAGE_INPUT,
                task,
                params,
                "axis",
                "axis is outside the tensor rank",
                {"received": axis, "rank": len(shape)},
            )
        )
        return PlannedExecution(None, None, diagnostics, degradations, True)  # type: ignore[arg-type]
    normalized_axis = int(axis) % len(shape)
    axis_length = int(shape[normalized_axis])
    outer_count = int(tensor.size // axis_length)

    eps = task.eps
    if not _is_number(eps) or not math.isfinite(float(eps)):
        diagnostics.append(
            _diag(
                E_EPS_INVALID,
                STAGE_INPUT,
                task,
                params,
                "eps",
                "eps must be a finite number; using eps_floor fallback",
                {"received": eps, "fallback": options.eps_floor},
            )
        )
        eps = options.eps_floor
        degradations.append(
            degradation(
                "EPS_FLOOR",
                STAGE_INPUT,
                E_EPS_INVALID,
                task.job_id,
                [],
                f"replace invalid eps with {options.eps_floor!r}",
            )
        )
    elif float(eps) <= 0:
        diagnostics.append(
            _diag(
                E_EPS_INVALID,
                STAGE_INPUT,
                task,
                params,
                "eps",
                "eps must be positive; using eps_floor fallback",
                {"received": eps, "fallback": options.eps_floor},
            )
        )
        eps = options.eps_floor
        degradations.append(
            degradation(
                "EPS_FLOOR",
                STAGE_INPUT,
                E_EPS_INVALID,
                task.job_id,
                [],
                f"replace non-positive eps with {options.eps_floor!r}",
            )
        )

    if op_kind == "layernorm" and norm_mode == "unbiased" and axis_length < 2:
        diagnostics.append(
            _diag(
                E_SHAPE_INVALID,
                STAGE_INPUT,
                task,
                params,
                "input_shape",
                "unbiased variance requires at least two elements along axis",
                {"axis_length": axis_length},
            )
        )
        return PlannedExecution(None, None, diagnostics, degradations, True)  # type: ignore[arg-type]

    if not np.all(np.isfinite(tensor)):
        diagnostics.append(
            _diag(
                E_INPUT_NONFINITE,
                STAGE_INPUT,
                task,
                params,
                "features",
                "input features contain NaN or infinity",
                {},
            )
        )
        return PlannedExecution(None, None, diagnostics, degradations, True)  # type: ignore[arg-type]

    context = ValidationContext(
        axis=normalized_axis,
        axis_length=axis_length,
        outer_count=outer_count,
        eps=float(eps),
        norm_mode=str(norm_mode),
        tensor_dtype=dtype,
    )
    plan, plan_diagnostics, plan_degradations, hard_error = _build_plan(
        task, op_kind, context, options, params
    )
    diagnostics.extend(plan_diagnostics)
    degradations.extend(plan_degradations)
    return PlannedExecution(context, plan, diagnostics, degradations, hard_error)


def _build_plan(
    task: OperatorTask,
    op_kind: str,
    context: ValidationContext,
    options: ExecutionOptions,
    params: dict[str, Any],
) -> tuple[BlockPlan | None, list[Diagnostic], list[dict[str, Any]], bool]:
    diagnostics: list[Diagnostic] = []
    degradations: list[dict[str, Any]] = []
    block_size = task.block_size
    requested_block_size: int | None
    if _is_int(block_size) and int(block_size) > 0:
        effective = int(block_size)
        requested_block_size = effective
    else:
        requested_block_size = None
        effective = 1
        diagnostics.append(
            _diag(
                E_BLOCK_SIZE_INVALID,
                STAGE_BLOCK_STATS,
                task,
                params,
                "block_size",
                "block_size must be a positive integer; using fallback block size 1",
                {"received": block_size, "fallback_block_size": 1},
            )
        )
        degradations.append(
            degradation(
                "BLOCK_SIZE_FALLBACK",
                STAGE_BLOCK_STATS,
                E_BLOCK_SIZE_INVALID,
                task.job_id,
                [],
                "continue with explicit block size 1",
            )
        )

    limits = options.limits
    if limits.max_block_elements is not None:
        if context.outer_count > limits.max_block_elements:
            diagnostics.append(
                _diag(
                    E_RESOURCE_LIMIT,
                    STAGE_BLOCK_STATS,
                    task,
                    params,
                    "max_block_elements",
                    "one element per outer slice already exceeds the block element limit",
                    {
                        "outer_count": context.outer_count,
                        "max_block_elements": limits.max_block_elements,
                    },
                )
            )
            return None, diagnostics, degradations, True
        max_chunk = max(1, limits.max_block_elements // max(1, context.outer_count))
        if effective > max_chunk:
            if not limits.allow_block_shrink:
                diagnostics.append(
                    _diag(
                        E_RESOURCE_LIMIT,
                        STAGE_BLOCK_STATS,
                        task,
                        params,
                        "block_size",
                        "block resident element limit exceeded and shrinking is disabled",
                        {
                            "requested_block_size": effective,
                            "max_block_elements": limits.max_block_elements,
                            "outer_count": context.outer_count,
                        },
                    )
                )
                return None, diagnostics, degradations, True
            diagnostics.append(
                _diag(
                    E_RESOURCE_LIMIT,
                    STAGE_BLOCK_STATS,
                    task,
                    params,
                    "block_size",
                    "block resident element limit exceeded; shrinking block",
                    {
                        "requested_block_size": effective,
                        "effective_block_size": max_chunk,
                        "max_block_elements": limits.max_block_elements,
                    },
                )
            )
            effective = max_chunk
            degradations.append(
                degradation(
                    "BLOCK_SHRINK",
                    STAGE_BLOCK_STATS,
                    E_RESOURCE_LIMIT,
                    task.job_id,
                    [],
                    "shrink block before any block is written",
                )
            )

    ranges = tuple(
        (start, min(start + effective, context.axis_length))
        for start in range(0, context.axis_length, effective)
    )
    if limits.max_blocks is not None and len(ranges) > limits.max_blocks:
        diagnostics.append(
            _diag(
                E_RESOURCE_LIMIT,
                STAGE_BLOCK_STATS,
                task,
                params,
                "block_size",
                "block count limit exceeded; no partial output was produced",
                {
                    "block_count": len(ranges),
                    "max_blocks": limits.max_blocks,
                    "effective_block_size": effective,
                },
            )
        )
        return None, diagnostics, degradations, True

    stats_floats_per_block = 8 if op_kind == "layernorm" else 7
    requested_stats_bytes = (
        context.outer_count * len(ranges) * stats_floats_per_block * 8
    )
    stats_dtype = "float64"
    stats_bytes = requested_stats_bytes
    if limits.max_stat_bytes is not None and stats_bytes > limits.max_stat_bytes:
        float32_bytes = (
            context.outer_count * len(ranges) * stats_floats_per_block * 4
        )
        if limits.allow_float32_stats and float32_bytes <= limits.max_stat_bytes:
            diagnostics.append(
                _diag(
                    E_RESOURCE_LIMIT,
                    STAGE_BLOCK_STATS,
                    task,
                    params,
                    "intermediate_statistics",
                    "intermediate statistics memory limit exceeded; using float32 statistics",
                    {
                        "estimated_float64_bytes": requested_stats_bytes,
                        "estimated_float32_bytes": float32_bytes,
                        "max_stat_bytes": limits.max_stat_bytes,
                    },
                )
            )
            stats_dtype = "float32"
            stats_bytes = float32_bytes
            degradations.append(
                degradation(
                    "FLOAT32_STATS",
                    STAGE_BLOCK_STATS,
                    E_RESOURCE_LIMIT,
                    task.job_id,
                    [],
                    "use float32 statistics before block execution starts",
                )
            )
        else:
            diagnostics.append(
                _diag(
                    E_RESOURCE_LIMIT,
                    STAGE_BLOCK_STATS,
                    task,
                    params,
                    "intermediate_statistics",
                    "intermediate statistics memory limit exceeded",
                    {
                        "estimated_float64_bytes": requested_stats_bytes,
                        "estimated_float32_bytes": float32_bytes,
                        "max_stat_bytes": limits.max_stat_bytes,
                    },
                )
            )
            return None, diagnostics, degradations, True

    plan = BlockPlan(
        axis=context.axis,
        axis_length=context.axis_length,
        outer_count=context.outer_count,
        requested_block_size=requested_block_size,
        effective_block_size=effective,
        block_ranges=ranges,
        stats_dtype=stats_dtype,
        stats_memory_bytes=stats_bytes,
        requested_stats_memory_bytes=requested_stats_bytes,
        max_block_elements_seen=context.outer_count * effective,
    )
    return plan, diagnostics, degradations, False


def stage_index_checksum(indices: set[int] | list[int]) -> dict[str, Any]:
    ordered = sorted(int(item) for item in indices)
    return {
        "count": len(ordered),
        "checksum": sha256_json(ordered),
    }
