"""Unified blocked Softmax and LayerNorm execution."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .errors import (
    E_INCONSISTENT,
    E_STAT_OVERFLOW,
    E_TOLERANCE_EXCEEDED,
    E_UNATTRIBUTED,
    E_WRITE_INDEX,
    STAGE_BLOCK_STATS,
    STAGE_COMPARISON,
    STAGE_INPUT,
    STAGE_NORMALIZATION,
    STAGE_UNATTRIBUTED,
    STAGE_WRITEBACK,
    Diagnostic,
    degradation,
)
from .models import (
    BlockRecord,
    ExecutionOptions,
    OperatorResult,
    OperatorTask,
)
from .planner import BlockPlan, ValidationContext, plan_execution, stage_index_checksum
from .selfcheck import run_self_check
from .tolerance import compare_arrays
from .util import array_sha256


def _index_rows(shape: tuple[int, ...], axis: int) -> np.ndarray:
    indices = np.arange(int(np.prod(shape)), dtype=np.int64).reshape(shape)
    return np.moveaxis(indices, axis, -1).reshape(-1, shape[axis])


def _reference_softmax(tensor: np.ndarray, axis: int) -> np.ndarray:
    values = np.asarray(tensor, dtype=np.float64)
    shifted = values - np.max(values, axis=axis, keepdims=True)
    exponentials = np.exp(shifted)
    return exponentials / np.sum(exponentials, axis=axis, keepdims=True)


def _reference_layernorm(
    tensor: np.ndarray, axis: int, eps: float, norm_mode: str
) -> np.ndarray:
    values = np.asarray(tensor, dtype=np.float64)
    ddof = 1 if norm_mode == "unbiased" else 0
    mean = np.mean(values, axis=axis, keepdims=True)
    variance = np.var(values, axis=axis, ddof=ddof, keepdims=True)
    return (values - mean) / np.sqrt(variance + eps)


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


def _overflow_triggered(values: np.ndarray, scale: float | None) -> bool:
    if scale is None or scale == 1.0:
        return False
    if not np.isfinite(scale) or abs(float(scale)) > 1e100:
        return True
    with np.errstate(over="ignore", invalid="ignore"):
        scaled = np.asarray(values, dtype=np.float64) * float(scale)
    return bool(
        np.any(~np.isfinite(scaled))
        or np.any(np.abs(scaled) > 1e100)
    )


def _write_blocks(
    *,
    blocks: list[BlockRecord],
    output_size: int,
    task: OperatorTask,
    options: ExecutionOptions,
    params: dict[str, Any],
    diagnostics: list[Diagnostic],
    degradations: list[dict[str, Any]],
) -> tuple[np.ndarray, set[int], bool]:
    output = np.zeros(output_size, dtype=np.float64)
    block_ids = [block.block_id for block in blocks]
    offset = int(options.faults.write_index_offset)
    injection_triggered = False
    first_invalid: dict[str, Any] | None = None

    if offset != 0:
        for block in blocks:
            proposed = np.asarray(block.element_indices, dtype=np.int64) + offset
            invalid = proposed[(proposed < 0) | (proposed >= output_size)]
            if invalid.size:
                first_invalid = {
                    "block_id": block.block_id,
                    "offset": offset,
                    "invalid_index": int(invalid[0]),
                    "output_size": output_size,
                }
                injection_triggered = True
                break
        if first_invalid is None:
            all_proposed = np.concatenate(
                [
                    np.asarray(block.element_indices, dtype=np.int64) + offset
                    for block in blocks
                ]
            )
            if (
                np.unique(all_proposed).size != output_size
                or int(all_proposed.min()) != 0
                or int(all_proposed.max()) != output_size - 1
            ):
                injection_triggered = True
                first_invalid = {
                    "block_id": blocks[0].block_id,
                    "offset": offset,
                    "invalid_index": int(all_proposed[0]),
                    "output_size": output_size,
                    "reason": "shifted index set is not a complete permutation",
                }

    if injection_triggered:
        diagnostics.append(
            _diag(
                E_WRITE_INDEX,
                STAGE_WRITEBACK,
                task,
                params,
                "write_index_offset",
                "injected writeback index is outside the output or breaks coverage",
                first_invalid or {"offset": offset},
            )
        )
        degradations.append(
            degradation(
                "SAFE_WRITEBACK",
                STAGE_WRITEBACK,
                E_WRITE_INDEX,
                task.job_id,
                block_ids,
                "discard shifted indices and write every block through its canonical explicit indices",
            )
        )

    seen = np.zeros(output_size, dtype=np.bool_)
    for block in blocks:
        indices = np.asarray(block.element_indices, dtype=np.int64)
        values = np.asarray(block.output_values, dtype=np.float64)
        if indices.size != values.size:
            diagnostics.append(
                _diag(
                    E_WRITE_INDEX,
                    STAGE_WRITEBACK,
                    task,
                    params,
                    "element_indices",
                    "block index count does not match its output value count",
                    {
                        "block_id": block.block_id,
                        "index_count": int(indices.size),
                        "value_count": int(values.size),
                    },
                )
            )
            continue
        valid = (indices >= 0) & (indices < output_size)
        if not np.all(valid):
            diagnostics.append(
                _diag(
                    E_WRITE_INDEX,
                    STAGE_WRITEBACK,
                    task,
                    params,
                    "element_indices",
                    "canonical block index is outside the output",
                    {
                        "block_id": block.block_id,
                        "invalid_index": int(indices[~valid][0]),
                    },
                )
            )
            continue
        already_seen = seen[indices]
        if np.any(already_seen):
            diagnostics.append(
                _diag(
                    E_WRITE_INDEX,
                    STAGE_WRITEBACK,
                    task,
                    params,
                    "element_indices",
                    "canonical block writes a duplicate output index",
                    {
                        "block_id": block.block_id,
                        "duplicate_index": int(indices[already_seen][0]),
                    },
                )
            )
            continue
        output[indices] = values
        seen[indices] = True

    written = set(int(item) for item in np.flatnonzero(seen))
    complete = len(written) == output_size
    if not complete:
        diagnostics.append(
            _diag(
                E_WRITE_INDEX,
                STAGE_WRITEBACK,
                task,
                params,
                "output_indices",
                "writeback did not cover every output element",
                {
                    "expected_elements": output_size,
                    "written_elements": len(written),
                    "missing_count": output_size - len(written),
                },
            )
        )
    return output, written, complete


def _block_rows(
    plan: BlockPlan, index_rows: np.ndarray
) -> list[np.ndarray]:
    rows: list[np.ndarray] = []
    for start, end in plan.block_ranges:
        rows.append(index_rows[:, start:end])
    return rows


def _softmax_pipeline(
    *,
    task: OperatorTask,
    tensor: np.ndarray,
    options: ExecutionOptions,
    context: ValidationContext,
    plan: BlockPlan,
    params: dict[str, Any],
    diagnostics: list[Diagnostic],
    degradations: list[dict[str, Any]],
    stage_indices: dict[str, set[int]],
) -> tuple[np.ndarray, np.ndarray, list[BlockRecord], str, set[int], bool]:
    axis = context.axis
    values = np.asarray(tensor, dtype=np.float64)
    rows = np.moveaxis(values, axis, -1).reshape(-1, context.axis_length)
    index_rows = _index_rows(values.shape, axis)
    stats_dtype = np.float32 if plan.stats_dtype == "float32" else np.float64
    work_rows = rows.astype(stats_dtype, copy=False)

    running_max = np.full(context.outer_count, -np.inf, dtype=stats_dtype)
    running_denominator = np.zeros(context.outer_count, dtype=stats_dtype)
    running_index = np.full(context.outer_count, -1, dtype=np.int64)
    block_records: list[BlockRecord] = []

    for block_number, (start, end) in enumerate(plan.block_ranges):
        block_values = work_rows[:, start:end]
        block_max = np.max(block_values, axis=1)
        local_maximum = np.argmax(block_values, axis=1).astype(np.int64)
        block_maximum_index = start + local_maximum
        wins = block_max > running_max
        wins |= (block_max == running_max) & (block_maximum_index < running_index)
        new_max = np.where(wins, block_max, running_max)
        with np.errstate(invalid="ignore"):
            rescale = np.exp(running_max - new_max)
        block_exponential_sum = np.sum(
            np.exp(block_values - new_max[:, None]),
            axis=1,
            dtype=stats_dtype,
        )
        new_denominator = running_denominator * rescale + block_exponential_sum
        new_index = np.where(wins, block_maximum_index, running_index)

        row_stats: list[dict[str, Any]] = []
        for row_index in range(context.outer_count):
            row_stats.append(
                {
                    "row_index": row_index,
                    "element_indices": [
                        int(item) for item in index_rows[row_index, start:end]
                    ],
                    "block_maximum": float(block_max[row_index]),
                    "first_maximum_axis_index": int(block_maximum_index[row_index]),
                    "first_maximum_flat_index": int(
                        index_rows[row_index, block_maximum_index[row_index]]
                    ),
                    "block_exponential_sum": float(
                        block_exponential_sum[row_index]
                    ),
                    "online_rescale_factor": float(rescale[row_index]),
                    "running_maximum_after": float(new_max[row_index]),
                    "running_denominator_after": float(
                        new_denominator[row_index]
                    ),
                    "running_maximum_flat_index_after": int(new_index[row_index])
                    if new_index[row_index] >= 0
                    else None,
                }
            )

        stats = {
            "block_maximum": [float(item) for item in block_max],
            "first_maximum_axis_index": [int(item) for item in block_maximum_index],
            "block_exponential_sum": [
                float(item) for item in block_exponential_sum
            ],
            "online_rescale_factor": [float(item) for item in rescale],
            "running_maximum_after": [float(item) for item in new_max],
            "running_denominator_after": [
                float(item) for item in new_denominator
            ],
            "tie_break_rule": "equal maxima choose the smallest axis element index",
        }
        element_indices = [
            int(item) for item in index_rows[:, start:end].reshape(-1).tolist()
        ]
        block_records.append(
            BlockRecord(
                block_id=f"b{block_number:04d}",
                axis_start=start,
                axis_end=end,
                element_indices=element_indices,
                rows=row_stats,
                statistics=stats,
                output_values=[],
            )
        )

        running_max = new_max.astype(stats_dtype, copy=False)
        running_denominator = new_denominator.astype(stats_dtype, copy=False)
        running_index = new_index

    if np.any(~np.isfinite(running_denominator)) or np.any(
        running_denominator <= 0
    ):
        diagnostics.append(
            _diag(
                E_STAT_OVERFLOW,
                STAGE_NORMALIZATION,
                task,
                params,
                "normalization_denominator",
                "online normalization denominator is not positive and finite",
                {
                    "minimum": float(np.min(running_denominator)),
                    "maximum": float(np.max(running_denominator)),
                },
            )
        )

    overflow_fault = _overflow_triggered(
        np.asarray(running_denominator, dtype=np.float64),
        options.faults.stat_overflow_scale,
    )
    float32_normalization = False
    if overflow_fault:
        diagnostics.append(
            _diag(
                E_STAT_OVERFLOW,
                STAGE_NORMALIZATION,
                task,
                params,
                "normalization_denominator",
                "injected statistic scale exceeded the finite-safe threshold; using float32 normalization",
                {
                    "stat_overflow_scale": options.faults.stat_overflow_scale,
                    "threshold": 1e100,
                    "max_denominator": float(np.max(running_denominator)),
                },
            )
        )
        degradations.append(
            degradation(
                "FLOAT32_NORMALIZATION",
                STAGE_NORMALIZATION,
                E_STAT_OVERFLOW,
                task.job_id,
                [block.block_id for block in block_records],
                "preserve raw block statistics and replace normalization with the float32 path",
            )
        )
        float32_normalization = True

    if float32_normalization:
        fallback_rows = rows.astype(np.float32)
        fallback_max = np.max(fallback_rows, axis=1)
        fallback_denominator = np.sum(
            np.exp(fallback_rows - fallback_max[:, None]),
            axis=1,
            dtype=np.float32,
        )
        output_rows = (
            np.exp(fallback_rows - fallback_max[:, None]) / fallback_denominator[:, None]
        ).astype(np.float64)
        compute_dtype = "float32"
    else:
        output_rows = (
            np.exp(work_rows - running_max[:, None])
            / running_denominator[:, None]
        ).astype(np.float64)
        compute_dtype = "float32" if plan.stats_dtype == "float32" else "float64"

    for block_number, (start, end) in enumerate(plan.block_ranges):
        block_records[block_number].output_values = [
            float(item) for item in output_rows[:, start:end].reshape(-1)
        ]

    output_flat, written, complete = _write_blocks(
        blocks=block_records,
        output_size=int(values.size),
        task=task,
        options=options,
        params=params,
        diagnostics=diagnostics,
        degradations=degradations,
    )
    output = output_flat.reshape(values.shape)
    stage_indices[STAGE_BLOCK_STATS] = set(range(int(values.size)))
    stage_indices[STAGE_NORMALIZATION] = set(range(int(values.size)))
    stage_indices[STAGE_WRITEBACK] = written
    reference = _reference_softmax(values, axis)
    return output, reference, block_records, compute_dtype, written, complete


def _layernorm_pipeline(
    *,
    task: OperatorTask,
    tensor: np.ndarray,
    options: ExecutionOptions,
    context: ValidationContext,
    plan: BlockPlan,
    params: dict[str, Any],
    diagnostics: list[Diagnostic],
    degradations: list[dict[str, Any]],
    stage_indices: dict[str, set[int]],
) -> tuple[np.ndarray, np.ndarray, list[BlockRecord], str, set[int], bool]:
    axis = context.axis
    values = np.asarray(tensor, dtype=np.float64)
    rows = np.moveaxis(values, axis, -1).reshape(-1, context.axis_length)
    index_rows = _index_rows(values.shape, axis)
    stats_dtype = np.float32 if plan.stats_dtype == "float32" else np.float64
    work_rows = rows.astype(stats_dtype, copy=False)

    running_count = np.zeros(context.outer_count, dtype=np.int64)
    running_mean = np.zeros(context.outer_count, dtype=stats_dtype)
    running_m2 = np.zeros(context.outer_count, dtype=stats_dtype)
    block_records: list[BlockRecord] = []

    for block_number, (start, end) in enumerate(plan.block_ranges):
        block_values = work_rows[:, start:end]
        block_count = end - start
        block_mean = np.mean(block_values, axis=1, dtype=stats_dtype)
        centered = block_values - block_mean[:, None]
        block_m2 = np.sum(centered * centered, axis=1, dtype=stats_dtype)
        block_variance = block_m2 / np.asarray(block_count, dtype=stats_dtype)

        total_count = running_count + block_count
        delta = block_mean - running_mean
        merge_weight = np.asarray(block_count, dtype=stats_dtype) / total_count.astype(
            stats_dtype
        )
        new_mean = running_mean + delta * merge_weight
        cross_term = (
            delta
            * delta
            * running_count.astype(stats_dtype)
            * np.asarray(block_count, dtype=stats_dtype)
            / total_count.astype(stats_dtype)
        )
        new_m2 = running_m2 + block_m2 + cross_term

        row_stats: list[dict[str, Any]] = []
        for row_index in range(context.outer_count):
            merged_variance = (
                float(new_m2[row_index]) / int(total_count[row_index])
                if int(total_count[row_index]) > 0
                else 0.0
            )
            row_stats.append(
                {
                    "row_index": row_index,
                    "element_indices": [
                        int(item) for item in index_rows[row_index, start:end]
                    ],
                    "block_count": int(block_count),
                    "block_mean": float(block_mean[row_index]),
                    "block_variance": float(block_variance[row_index]),
                    "block_m2": float(block_m2[row_index]),
                    "running_count_after": int(total_count[row_index]),
                    "running_mean_after": float(new_mean[row_index]),
                    "running_m2_after": float(new_m2[row_index]),
                    "merged_variance_after": merged_variance,
                    "merge_delta": float(delta[row_index]),
                }
            )
        stats = {
            "block_mean": [float(item) for item in block_mean],
            "block_variance": [float(item) for item in block_variance],
            "block_m2": [float(item) for item in block_m2],
            "running_mean_after": [float(item) for item in new_mean],
            "running_m2_after": [float(item) for item in new_m2],
            "reduction_rule": "Chan/Welford cross-block merge in ascending block order",
        }
        element_indices = [
            int(item) for item in index_rows[:, start:end].reshape(-1).tolist()
        ]
        block_records.append(
            BlockRecord(
                block_id=f"b{block_number:04d}",
                axis_start=start,
                axis_end=end,
                element_indices=element_indices,
                rows=row_stats,
                statistics=stats,
                output_values=[],
            )
        )
        running_count = total_count
        running_mean = new_mean.astype(stats_dtype, copy=False)
        running_m2 = new_m2.astype(stats_dtype, copy=False)

    ddof = 1 if context.norm_mode == "unbiased" else 0
    variance_denominator = context.axis_length - ddof
    variance = (
        np.asarray(running_m2, dtype=np.float64) / float(variance_denominator)
    )
    variance = np.maximum(variance, 0.0)
    mean = np.asarray(running_mean, dtype=np.float64)
    numerator = rows - mean[:, None]
    denominator = np.sqrt(variance + context.eps)
    if np.any(~np.isfinite(denominator)) or np.any(denominator <= 0):
        diagnostics.append(
            _diag(
                E_STAT_OVERFLOW,
                STAGE_NORMALIZATION,
                task,
                params,
                "normalization_denominator",
                "layernorm denominator is not positive and finite",
                {
                    "min_variance": float(np.min(variance)),
                    "min_denominator": float(np.min(denominator)),
                },
            )
        )

    overflow_fault = _overflow_triggered(
        variance, options.faults.stat_overflow_scale
    )
    float32_normalization = False
    if overflow_fault:
        diagnostics.append(
            _diag(
                E_STAT_OVERFLOW,
                STAGE_NORMALIZATION,
                task,
                params,
                "variance",
                "injected statistic scale exceeded the finite-safe threshold; using float32 normalization",
                {
                    "stat_overflow_scale": options.faults.stat_overflow_scale,
                    "threshold": 1e100,
                    "max_variance": float(np.max(variance)),
                },
            )
        )
        degradations.append(
            degradation(
                "FLOAT32_NORMALIZATION",
                STAGE_NORMALIZATION,
                E_STAT_OVERFLOW,
                task.job_id,
                [block.block_id for block in block_records],
                "preserve raw block statistics and replace normalization with the float32 path",
            )
        )
        float32_normalization = True

    if float32_normalization:
        fallback_rows = rows.astype(np.float32)
        fallback_mean = np.mean(fallback_rows, axis=1, dtype=np.float32)
        fallback_variance = np.var(
            fallback_rows, axis=1, ddof=ddof, dtype=np.float32
        )
        output_rows = (
            (fallback_rows - fallback_mean[:, None])
            / np.sqrt(fallback_variance + np.float32(context.eps))[:, None]
        ).astype(np.float64)
        compute_dtype = "float32"
    else:
        output_rows = (numerator / denominator[:, None]).astype(np.float64)
        compute_dtype = "float32" if plan.stats_dtype == "float32" else "float64"

    for block_number, (start, end) in enumerate(plan.block_ranges):
        block_records[block_number].output_values = [
            float(item) for item in output_rows[:, start:end].reshape(-1)
        ]

    output_flat, written, complete = _write_blocks(
        blocks=block_records,
        output_size=int(values.size),
        task=task,
        options=options,
        params=params,
        diagnostics=diagnostics,
        degradations=degradations,
    )
    output = output_flat.reshape(values.shape)
    stage_indices[STAGE_BLOCK_STATS] = set(range(int(values.size)))
    stage_indices[STAGE_NORMALIZATION] = set(range(int(values.size)))
    stage_indices[STAGE_WRITEBACK] = written
    reference = _reference_layernorm(
        values, axis, context.eps, context.norm_mode
    )
    return output, reference, block_records, compute_dtype, written, complete


def _execute_core(
    task: OperatorTask,
    tensor: np.ndarray,
    options: ExecutionOptions,
) -> OperatorResult:
    planned = plan_execution(task, tensor, options)
    params = task.parameter_summary()
    params["faults"] = options.faults.to_dict()
    diagnostics = list(planned.diagnostics)
    degradations = list(planned.degradations)

    if planned.hard_error or planned.plan is None or planned.context is None:
        if diagnostics:
            degradations.append(
                degradation(
                    "ABORT_JOB",
                    diagnostics[0].stage,
                    diagnostics[0].code,
                    task.job_id,
                    [],
                    "return a failed result without executing or writing downstream blocks",
                )
            )
        return OperatorResult(
            job_id=task.job_id,
            task=task,
            op_kind=task.op_kind if isinstance(task.op_kind, str) else None,
            status="failed",
            output=None,
            reference=None,
            plan={},
            blocks=[],
            diagnostics=diagnostics,
            degradations=degradations,
            tolerance_report=None,
            stage_indices={STAGE_INPUT: set(range(int(tensor.size)))},
            self_check={
                "status": "not_applicable",
                "reason": "execution aborted before block statistics",
            },
            compute_dtype="not_started",
        )

    plan = planned.plan
    context = planned.context
    block_ids = [
        f"b{index:04d}" for index in range(len(plan.block_ranges))
    ]
    for item in degradations:
        if not item["affected_blocks"]:
            item["affected_blocks"] = list(block_ids)

    stage_indices: dict[str, set[int]] = {
        STAGE_INPUT: set(range(int(tensor.size)))
    }
    if task.op_kind == "softmax":
        output, reference, blocks, compute_dtype, _, complete = _softmax_pipeline(
            task=task,
            tensor=tensor,
            options=options,
            context=context,
            plan=plan,
            params=params,
            diagnostics=diagnostics,
            degradations=degradations,
            stage_indices=stage_indices,
        )
    else:
        output, reference, blocks, compute_dtype, _, complete = _layernorm_pipeline(
            task=task,
            tensor=tensor,
            options=options,
            context=context,
            plan=plan,
            params=params,
            diagnostics=diagnostics,
            degradations=degradations,
            stage_indices=stage_indices,
        )

    tolerance_report = compare_arrays(output, reference, options.tolerance)
    if not tolerance_report["within_tolerance"]:
        diagnostics.append(
            _diag(
                E_TOLERANCE_EXCEEDED,
                STAGE_COMPARISON,
                task,
                params,
                "output",
                "blocked output exceeds the configured float64 tolerance",
                {
                    "max_absolute_deviation": tolerance_report[
                        "max_absolute_deviation"
                    ],
                    "max_relative_deviation": tolerance_report[
                        "max_relative_deviation"
                    ],
                    "violation_count": tolerance_report["violation_count"],
                    "first_violation": (
                        tolerance_report["violations"][0]
                        if tolerance_report["violations"]
                        else None
                    ),
                },
            )
        )

    status = "failed" if not tolerance_report["within_tolerance"] or not complete else (
        "degraded" if degradations else "ok"
    )
    result = OperatorResult(
        job_id=task.job_id,
        task=task,
        op_kind=task.op_kind,
        status=status,
        output=output,
        reference=reference,
        plan={
            **plan.to_dict(),
            "output_sha256": array_sha256(output),
            "reference_sha256": array_sha256(reference),
            "stage_indices": {
                stage: stage_index_checksum(indices)
                for stage, indices in sorted(stage_indices.items())
            },
        },
        blocks=blocks,
        diagnostics=diagnostics,
        degradations=degradations,
        tolerance_report=tolerance_report,
        stage_indices=stage_indices,
        compute_dtype=compute_dtype,
    )
    result.self_check = run_self_check(result, options.tolerance)
    if result.self_check["status"] != "passed":
        result.diagnostics.append(
            _diag(
                E_INCONSISTENT,
                "cross_module_self_check",
                task,
                params,
                "self_check",
                "cross-module consistency self-check failed",
                {
                    "categories": result.self_check.get("failed_categories", []),
                },
            )
        )
        result.status = "failed"
    return result


def execute_job(
    task: OperatorTask | Mapping[str, Any],
    tensor: np.ndarray,
    options: ExecutionOptions | None = None,
) -> OperatorResult:
    """Execute one task through the unified blocked entry point."""

    if not isinstance(task, OperatorTask):
        task = OperatorTask.from_mapping(task)
    array = np.asarray(tensor)
    options = options if options is not None else ExecutionOptions()
    try:
        return _execute_core(task, array, options)
    except Exception as exc:  # No stage claimed this exception; expose it in the result.
        params = task.parameter_summary()
        params["faults"] = options.faults.to_dict()
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        diagnostic = Diagnostic(
            code=E_UNATTRIBUTED,
            stage=STAGE_UNATTRIBUTED,
            job_id=task.job_id,
            field=None,
            message="unhandled exception was not attributed to a stage",
            params=params,
            details=error,
        )
        return OperatorResult(
            job_id=task.job_id,
            task=task,
            op_kind=task.op_kind if isinstance(task.op_kind, str) else None,
            status="failed",
            output=None,
            reference=None,
            plan={},
            blocks=[],
            diagnostics=[diagnostic],
            degradations=[],
            tolerance_report=None,
            stage_indices={},
            self_check={
                "status": "not_applicable",
                "reason": "unattributed exception",
            },
            unattributed_errors=[error],
        )


def softmax(
    tensor: np.ndarray,
    axis: int = -1,
    block_size: int | None = None,
    eps: float = 1e-9,
    dtype: str | None = None,
    seed: int = 0,
    norm_mode: str = "stable",
    job_id: str = "softmax",
    options: ExecutionOptions | None = None,
) -> OperatorResult:
    array = np.asarray(tensor)
    effective_dtype = dtype if dtype is not None else array.dtype.name
    effective_block = block_size
    if effective_block is None:
        try:
            effective_block = (
                int(array.shape[axis]) if isinstance(axis, (int, np.integer)) else 1
            )
        except (IndexError, TypeError):
            effective_block = 1
    task = {
        "job_id": job_id,
        "op_kind": "softmax",
        "input_shape": list(array.shape),
        "block_size": effective_block,
        "axis": axis,
        "eps": eps,
        "dtype": effective_dtype,
        "seed": seed,
        "norm_mode": norm_mode,
    }
    return execute_job(task, array, options)


def layernorm(
    tensor: np.ndarray,
    axis: int = -1,
    block_size: int | None = None,
    eps: float = 1e-5,
    dtype: str | None = None,
    seed: int = 0,
    norm_mode: str = "population",
    job_id: str = "layernorm",
    options: ExecutionOptions | None = None,
) -> OperatorResult:
    array = np.asarray(tensor)
    effective_dtype = dtype if dtype is not None else array.dtype.name
    effective_block = block_size
    if effective_block is None:
        try:
            effective_block = (
                int(array.shape[axis]) if isinstance(axis, (int, np.integer)) else 1
            )
        except (IndexError, TypeError):
            effective_block = 1
    task = {
        "job_id": job_id,
        "op_kind": "layernorm",
        "input_shape": list(array.shape),
        "block_size": effective_block,
        "axis": axis,
        "eps": eps,
        "dtype": effective_dtype,
        "seed": seed,
        "norm_mode": norm_mode,
    }
    return execute_job(task, array, options)
