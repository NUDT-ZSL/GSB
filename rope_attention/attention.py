"""Blocked causal attention with online softmax and mergeable statistics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import E_TOLERANCE_EXCEEDED, RopeError


@dataclass(frozen=True)
class NumericTolerance:
    atol: float = 1e-12
    rtol: float = 1e-10
    relative_floor: float = 1e-12

    def to_dict(self) -> dict[str, float]:
        return {
            "atol": self.atol,
            "relative_floor": self.relative_floor,
            "rtol": self.rtol,
        }


@dataclass(frozen=True)
class OnlineSoftmaxBlock:
    output: np.ndarray
    final_max: np.ndarray
    final_sum: np.ndarray
    block_stats: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class FullAttentionResult:
    output: np.ndarray
    scores: np.ndarray
    denominator: np.ndarray
    row_max: np.ndarray


@dataclass(frozen=True)
class AttentionComparison:
    max_abs_output_deviation: float
    max_rel_output_deviation: float
    max_abs_denominator_deviation: float
    max_rel_denominator_deviation: float
    within_tolerance: bool
    tolerance: NumericTolerance

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_abs_denominator_deviation": self.max_abs_denominator_deviation,
            "max_abs_output_deviation": self.max_abs_output_deviation,
            "max_rel_denominator_deviation": self.max_rel_denominator_deviation,
            "max_rel_output_deviation": self.max_rel_output_deviation,
            "tolerance": self.tolerance.to_dict(),
            "within_tolerance": self.within_tolerance,
        }


@dataclass(frozen=True)
class BlockedAttentionResult:
    output: np.ndarray
    block_outputs: tuple[np.ndarray, ...]
    block_max: tuple[np.ndarray, ...]
    block_sum: tuple[np.ndarray, ...]
    block_stats: tuple[tuple[dict[str, Any], ...], ...]


def query_blocks(seq_len: int, block_size: int) -> list[tuple[int, int]]:
    if seq_len <= 0 or block_size <= 0:
        raise ValueError("seq_len and block_size must be positive")
    return [
        (start, min(start + block_size, seq_len))
        for start in range(0, seq_len, block_size)
    ]


def key_prefix_length(q_end: int, seq_len: int, causal: bool) -> int:
    return q_end if causal else seq_len


def compute_score_block(
    q_rot: np.ndarray,
    k_rot: np.ndarray,
    *,
    q_start: int,
    q_end: int,
    key_end: int,
    causal: bool,
    head_dim: int,
) -> np.ndarray:
    """Compute one query block against its key prefix, applying the mask."""

    q_block = np.ascontiguousarray(q_rot[:, q_start:q_end, :], dtype=np.float64)
    k_block = np.ascontiguousarray(k_rot[:, :key_end, :], dtype=np.float64)
    scores = np.matmul(q_block, k_block.transpose(0, 2, 1))
    scores = scores / np.sqrt(float(head_dim))
    if causal:
        q_positions = np.arange(q_start, q_end, dtype=np.int64)[:, None]
        k_positions = np.arange(0, key_end, dtype=np.int64)[None, :]
        scores = np.where(
            k_positions <= q_positions,
            scores,
            -np.inf,
        )
    return np.ascontiguousarray(scores, dtype=np.float64)


def online_softmax(
    scores: np.ndarray,
    v: np.ndarray,
    *,
    block_size: int,
    causal: bool,
    q_start: int,
) -> OnlineSoftmaxBlock:
    """Stream key blocks while maintaining a running max, denominator and output."""

    scores64 = np.ascontiguousarray(scores, dtype=np.float64)
    v64 = np.ascontiguousarray(v, dtype=np.float64)
    if scores64.ndim != 3:
        raise ValueError("scores must have shape (heads, query, key)")
    if v64.shape[:2] != (scores64.shape[0], scores64.shape[2]):
        raise ValueError("V shape does not align with score keys")
    heads, q_len, key_len = scores64.shape
    head_dim = v64.shape[2]
    running_max = np.full((heads, q_len), -np.inf, dtype=np.float64)
    running_sum = np.zeros((heads, q_len), dtype=np.float64)
    accumulator = np.zeros((heads, q_len, head_dim), dtype=np.float64)
    stats: list[dict[str, Any]] = []
    key_start = 0
    block_index = 0
    while key_start < key_len:
        key_end = min(key_start + block_size, key_len)
        logits = scores64[:, :, key_start:key_end]
        block_max = np.max(logits, axis=-1)
        if np.isneginf(block_max).any():
            # A causal query block always has at least one valid key, so this
            # indicates a malformed score tensor rather than a normal boundary.
            raise RopeError(
                E_TOLERANCE_EXCEEDED,
                "an entire causal key block was masked",
                details={
                    "block_index": block_index,
                    "key_start": key_start,
                    "key_end": key_end,
                },
            )
        shifted = np.exp(logits - block_max[..., None])
        block_sum = np.sum(shifted, axis=-1)
        weighted = np.matmul(shifted, v64[:, key_start:key_end, :])
        stats.append(
            {
                "block_index": block_index,
                "key_end": key_end,
                "key_start": key_start,
                "max": np.ascontiguousarray(block_max, dtype=np.float64),
                "sum": np.ascontiguousarray(block_sum, dtype=np.float64),
            }
        )
        if block_index == 0:
            running_max = np.ascontiguousarray(block_max, dtype=np.float64)
            running_sum = np.ascontiguousarray(block_sum, dtype=np.float64)
            accumulator = np.ascontiguousarray(weighted, dtype=np.float64)
        else:
            next_max = np.maximum(running_max, block_max)
            old_scale = np.exp(running_max - next_max)
            new_scale = np.exp(block_max - next_max)
            running_sum = running_sum * old_scale + block_sum * new_scale
            accumulator = (
                accumulator * old_scale[..., None]
                + weighted * new_scale[..., None]
            )
            running_max = next_max
        key_start = key_end
        block_index += 1
    output = accumulator / running_sum[..., None]
    return OnlineSoftmaxBlock(
        output=np.ascontiguousarray(output, dtype=np.float64),
        final_max=np.ascontiguousarray(running_max, dtype=np.float64),
        final_sum=np.ascontiguousarray(running_sum, dtype=np.float64),
        block_stats=tuple(stats),
    )


def merge_softmax_stats(
    block_stats: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> tuple[np.ndarray, np.ndarray]:
    """Merge independent block (max, sum) statistics into one denominator."""

    if not block_stats:
        raise ValueError("at least one block statistic is required")
    maxima = [np.asarray(item["max"], dtype=np.float64) for item in block_stats]
    sums = [np.asarray(item["sum"], dtype=np.float64) for item in block_stats]
    shape = maxima[0].shape
    if any(item.shape != shape for item in maxima + sums):
        raise ValueError("block statistic shapes differ")
    global_max = np.max(np.stack(maxima, axis=0), axis=0)
    merged_sum = np.zeros(shape, dtype=np.float64)
    for block_max, block_sum in zip(maxima, sums):
        merged_sum += block_sum * np.exp(block_max - global_max)
    return (
        np.ascontiguousarray(global_max, dtype=np.float64),
        np.ascontiguousarray(merged_sum, dtype=np.float64),
    )


def full_attention(
    q_rot: np.ndarray,
    k_rot: np.ndarray,
    v: np.ndarray,
    *,
    causal: bool,
    head_dim: int,
) -> FullAttentionResult:
    """Unblocked reference path used only for numerical verification."""

    scores = compute_score_block(
        q_rot,
        k_rot,
        q_start=0,
        q_end=q_rot.shape[1],
        key_end=k_rot.shape[1],
        causal=causal,
        head_dim=head_dim,
    )
    row_max = np.max(scores, axis=-1)
    shifted = np.exp(scores - row_max[..., None])
    denominator = np.sum(shifted, axis=-1)
    probabilities = shifted / denominator[..., None]
    output = np.matmul(probabilities, np.ascontiguousarray(v, dtype=np.float64))
    return FullAttentionResult(
        output=np.ascontiguousarray(output, dtype=np.float64),
        scores=scores,
        denominator=np.ascontiguousarray(denominator, dtype=np.float64),
        row_max=np.ascontiguousarray(row_max, dtype=np.float64),
    )


def blocked_attention(
    q_rot: np.ndarray,
    k_rot: np.ndarray,
    v: np.ndarray,
    *,
    block_size: int,
    causal: bool,
    head_dim: int,
) -> BlockedAttentionResult:
    seq_len = q_rot.shape[1]
    outputs: list[np.ndarray] = []
    maxima: list[np.ndarray] = []
    sums: list[np.ndarray] = []
    all_stats: list[tuple[dict[str, Any], ...]] = []
    for q_start, q_end in query_blocks(seq_len, block_size):
        key_end = key_prefix_length(q_end, seq_len, causal)
        scores = compute_score_block(
            q_rot,
            k_rot,
            q_start=q_start,
            q_end=q_end,
            key_end=key_end,
            causal=causal,
            head_dim=head_dim,
        )
        online = online_softmax(
            scores,
            v[:, :key_end, :],
            block_size=block_size,
            causal=causal,
            q_start=q_start,
        )
        outputs.append(online.output)
        maxima.append(online.final_max)
        sums.append(online.final_sum)
        all_stats.append(online.block_stats)
    return BlockedAttentionResult(
        output=np.ascontiguousarray(
            np.concatenate(outputs, axis=1), dtype=np.float64
        ),
        block_outputs=tuple(outputs),
        block_max=tuple(maxima),
        block_sum=tuple(sums),
        block_stats=tuple(all_stats),
    )


def _max_relative_deviation(
    actual: np.ndarray,
    reference: np.ndarray,
    *,
    floor: float,
) -> float:
    difference = np.abs(actual - reference)
    denominator = np.maximum(np.abs(reference), floor)
    return float(np.max(difference / denominator))


def compare_blocked_to_full(
    blocked: BlockedAttentionResult,
    full: FullAttentionResult,
    *,
    tolerance: NumericTolerance,
) -> AttentionComparison:
    max_abs_output = float(np.max(np.abs(blocked.output - full.output)))
    max_rel_output = _max_relative_deviation(
        blocked.output,
        full.output,
        floor=tolerance.relative_floor,
    )
    merged_denominators: list[np.ndarray] = []
    for stats in blocked.block_stats:
        _, merged_sum = merge_softmax_stats(stats)
        merged_denominators.append(merged_sum)
    merged_denominator = np.concatenate(merged_denominators, axis=1)
    max_abs_denominator = float(
        np.max(np.abs(merged_denominator - full.denominator))
    )
    max_rel_denominator = _max_relative_deviation(
        merged_denominator,
        full.denominator,
        floor=tolerance.relative_floor,
    )
    within = (
        max_abs_output <= tolerance.atol
        + tolerance.rtol * float(np.max(np.abs(full.output)))
        and max_abs_denominator <= tolerance.atol
        + tolerance.rtol * float(np.max(np.abs(full.denominator)))
    )
    comparison = AttentionComparison(
        max_abs_output_deviation=max_abs_output,
        max_rel_output_deviation=max_rel_output,
        max_abs_denominator_deviation=max_abs_denominator,
        max_rel_denominator_deviation=max_rel_denominator,
        within_tolerance=within,
        tolerance=tolerance,
    )
    if not within:
        raise RopeError(
            E_TOLERANCE_EXCEEDED,
            "blocked attention deviates from the unblocked reference path",
            details=comparison.to_dict(),
        )
    return comparison

