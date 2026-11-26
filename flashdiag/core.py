"""Deterministic float64 online-softmax block attention and comparisons."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np

from .errors import DiagnosticError, E_TOLERANCE_EXCEEDED
from .models import ArraySet, BlockPlan


FLOAT64_TINY = float(np.finfo(np.float64).tiny)
EXP_MIN = math.log(FLOAT64_TINY)


@dataclass(frozen=True)
class ScoreBlock:
    head: int
    q_block: int
    kv_block: int
    q_start: int
    kv_start: int
    scores: np.ndarray
    row_max: np.ndarray
    row_sum: np.ndarray
    partial: np.ndarray


@dataclass(frozen=True)
class NormBlock:
    merged_max: np.ndarray
    merged_denom: np.ndarray
    merged_acc: np.ndarray


def _safe_exp(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    result = np.zeros_like(values, dtype=np.float64)
    finite = np.isfinite(values)
    np.exp(np.maximum(values, EXP_MIN), out=result, where=finite)
    return result


def compute_score_block(
    q_block: np.ndarray,
    k_block: np.ndarray,
    v_block: np.ndarray,
    *,
    head: int,
    q_block_index: int,
    kv_block_index: int,
    q_start: int,
    kv_start: int,
    causal: bool,
) -> ScoreBlock:
    """Compute one Q/KV score tile and its online-softmax block partials."""

    q_float = np.asarray(q_block, dtype=np.float64)
    k_float = np.asarray(k_block, dtype=np.float64)
    v_float = np.asarray(v_block, dtype=np.float64)
    if q_float.ndim != 2 or k_float.ndim != 2 or v_float.ndim != 2:
        raise ValueError("score block inputs must be rank-2")
    if q_float.shape[1] != k_float.shape[1] or k_float.shape != v_float.shape:
        raise ValueError("score block Q/K/V dimensions are inconsistent")

    scale = 1.0 / math.sqrt(q_float.shape[1])
    with np.errstate(over="ignore", under="ignore", invalid="ignore"):
        scores = (q_float @ k_float.T) * scale
        if causal:
            q_positions = q_start + np.arange(q_float.shape[0], dtype=np.int64)
            kv_positions = kv_start + np.arange(k_float.shape[0], dtype=np.int64)
            visible = kv_positions[None, :] <= q_positions[:, None]
            scores = np.where(visible, scores, -np.inf)

        row_max = np.max(scores, axis=1)
        row_max = np.where(np.isfinite(row_max), row_max, 0.0).astype(np.float64)
        shifted = scores - row_max[:, None]
        weights = _safe_exp(shifted)
        row_sum = np.sum(weights, axis=1, dtype=np.float64)
        row_sum = np.where(row_sum > 0.0, row_sum, FLOAT64_TINY)
        partial = weights @ v_float

    return ScoreBlock(
        head=head,
        q_block=q_block_index,
        kv_block=kv_block_index,
        q_start=q_start,
        kv_start=kv_start,
        scores=np.asarray(scores, dtype=np.float64),
        row_max=np.asarray(row_max, dtype=np.float64),
        row_sum=np.asarray(row_sum, dtype=np.float64),
        partial=np.asarray(partial, dtype=np.float64),
    )


def merge_score_blocks(blocks: Iterable[ScoreBlock | Mapping[str, Any]]) -> NormBlock:
    """Merge ordered score partials using the online-softmax recurrence."""

    merged_max: np.ndarray | None = None
    merged_denom: np.ndarray | None = None
    merged_acc: np.ndarray | None = None

    for block in blocks:
        if isinstance(block, ScoreBlock):
            block_max = block.row_max
            block_sum = block.row_sum
            block_partial = block.partial
        else:
            block_max = np.asarray(block["row_max"], dtype=np.float64)
            block_sum = np.asarray(block["row_sum"], dtype=np.float64)
            block_partial = np.asarray(block["partial"], dtype=np.float64)

        if merged_max is None:
            merged_max = block_max.copy()
            merged_denom = block_sum.copy()
            merged_acc = block_partial.copy()
            continue

        assert merged_denom is not None and merged_acc is not None
        new_max = np.maximum(merged_max, block_max)
        old_scale = _safe_exp(merged_max - new_max)
        block_scale = _safe_exp(block_max - new_max)
        merged_denom = merged_denom * old_scale + block_sum * block_scale
        merged_acc = merged_acc * old_scale[:, None] + block_partial * block_scale[:, None]
        merged_max = new_max

    if merged_max is None or merged_denom is None or merged_acc is None:
        raise ValueError("at least one score block is required")
    return NormBlock(
        merged_max=np.asarray(merged_max, dtype=np.float64),
        merged_denom=np.asarray(merged_denom, dtype=np.float64),
        merged_acc=np.asarray(merged_acc, dtype=np.float64),
    )


def normalize_block(norm: NormBlock) -> np.ndarray:
    denominator = np.maximum(
        np.asarray(norm.merged_denom, dtype=np.float64),
        FLOAT64_TINY,
    )
    return np.asarray(norm.merged_acc, dtype=np.float64) / denominator[:, None]


def compute_full_reference(arrays: ArraySet, causal: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Direct whole-sequence reference: output, row maxima, row denominators."""

    q_all, k_all, v_all = arrays.float64()
    num_heads, seq_len, head_dim = q_all.shape
    output = np.empty((num_heads, seq_len, head_dim), dtype=np.float64)
    maxima = np.empty((num_heads, seq_len), dtype=np.float64)
    denominators = np.empty((num_heads, seq_len), dtype=np.float64)
    scale = 1.0 / math.sqrt(head_dim)

    for head in range(num_heads):
        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            scores = (q_all[head] @ k_all[head].T) * scale
            if causal:
                positions = np.arange(seq_len, dtype=np.int64)
                visible = positions[None, :] <= positions[:, None]
                scores = np.where(visible, scores, -np.inf)
            local_max = np.max(scores, axis=1)
            local_max = np.where(np.isfinite(local_max), local_max, 0.0)
            weights = _safe_exp(scores - local_max[:, None])
            local_sum = np.sum(weights, axis=1, dtype=np.float64)
            local_sum = np.where(local_sum > 0.0, local_sum, FLOAT64_TINY)
            local_output = (weights @ v_all[head]) / local_sum[:, None]
        maxima[head] = local_max
        denominators[head] = local_sum
        output[head] = local_output
    return output, maxima, denominators


def compute_full_blockwise(
    arrays: ArraySet,
    plans: Iterable[BlockPlan],
    causal: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Recompute every Q block without cache; used for incremental parity audits."""

    q_all, k_all, v_all = arrays.float64()
    num_heads, seq_len, head_dim = q_all.shape
    output = np.empty((num_heads, seq_len, head_dim), dtype=np.float64)
    maxima = np.empty((num_heads, seq_len), dtype=np.float64)
    denominators = np.empty((num_heads, seq_len), dtype=np.float64)

    for plan in plans:
        q_slice = q_all[plan.head, plan.q_start : plan.q_start + plan.q_len]
        blocks: list[ScoreBlock] = []
        for kv_block in plan.kv_blocks:
            blocks.append(
                compute_score_block(
                    q_slice,
                    k_all[plan.head, kv_block.start : kv_block.stop],
                    v_all[plan.head, kv_block.start : kv_block.stop],
                    head=plan.head,
                    q_block_index=plan.q_block,
                    kv_block_index=kv_block.index,
                    q_start=plan.q_start,
                    kv_start=kv_block.start,
                    causal=causal,
                )
            )
        norm = merge_score_blocks(blocks)
        local_output = normalize_block(norm)
        q_slice_out = slice(plan.q_start, plan.q_start + plan.q_len)
        output[plan.head, q_slice_out] = local_output
        maxima[plan.head, q_slice_out] = norm.merged_max
        denominators[plan.head, q_slice_out] = norm.merged_denom
    return output, maxima, denominators


def first_array_difference(
    path: str,
    actual: np.ndarray,
    expected: np.ndarray,
    *,
    atol: float = 0.0,
    rtol: float = 0.0,
) -> dict[str, Any] | None:
    actual_array = np.asarray(actual)
    expected_array = np.asarray(expected)
    if actual_array.shape != expected_array.shape:
        return {
            "path": path,
            "kind": "shape",
            "actual_shape": list(actual_array.shape),
            "expected_shape": list(expected_array.shape),
        }
    if atol == 0.0 and rtol == 0.0:
        difference = actual_array != expected_array
    else:
        absolute = np.abs(actual_array - expected_array)
        tolerance = atol + rtol * np.abs(expected_array)
        difference = ~(absolute <= tolerance)
    positions = np.argwhere(difference)
    if positions.size == 0:
        return None
    index = tuple(int(component) for component in positions[0])
    actual_value = actual_array[index]
    expected_value = expected_array[index]
    if np.issubdtype(actual_array.dtype, np.number) and np.issubdtype(
        expected_array.dtype, np.number
    ):
        error: float | None = abs(float(actual_value) - float(expected_value))
    else:
        error = None
    return {
        "path": path,
        "kind": "value",
        "index": list(index),
        "actual": _json_scalar(actual_value),
        "expected": _json_scalar(expected_value),
        "abs_error": error,
    }


def _json_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    return value


def assert_arrays_equal(
    path: str,
    actual: np.ndarray,
    expected: np.ndarray,
    *,
    atol: float = 0.0,
    rtol: float = 0.0,
) -> None:
    difference = first_array_difference(
        path,
        actual,
        expected,
        atol=atol,
        rtol=rtol,
    )
    if difference is not None:
        raise DiagnosticError(
            E_TOLERANCE_EXCEEDED,
            f"array mismatch at {path}",
            first_difference=difference,
            atol=atol,
            rtol=rtol,
        )

