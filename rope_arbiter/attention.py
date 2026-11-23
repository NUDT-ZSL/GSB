"""Blockwise attention, online softmax, and cross-variant diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .hashing import hash_arrays, sha256_json
from .models import ResolvedCase


def estimate_attention_memory(case: ResolvedCase) -> dict[str, int]:
    """Conservative float64 peak estimates for one variant invocation."""

    heads = case.num_heads
    seq = case.seq_len
    dim = case.head_dim
    block = min(case.block_size, seq)
    block_scores = heads * block * block * 8
    block_state = heads * block * (dim + 2) * 8
    full_scores = heads * seq * seq * 8
    output = heads * seq * dim * 8
    block_resident = 4 * block_scores + 4 * block_state + output
    full_reference = 3 * full_scores + output
    return {
        "block_resident_bytes": int(block_resident),
        "full_reference_bytes": int(full_reference),
        "peak_bytes": int(block_resident + full_reference + output),
    }


@dataclass(frozen=True)
class BlockScoreResult:
    scores: np.ndarray
    blocks: np.ndarray
    query_block_starts: np.ndarray
    key_block_starts: np.ndarray
    effective_block_size: int
    block_content_hash: str
    memory_estimate: dict[str, int]
    block_score_max_abs_error: float
    first_score_mismatch: dict[str, Any] | None
    code: str

    def to_artifact(self) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
        meta = {
            "scores_shape": list(self.scores.shape),
            "blocks_shape": list(self.blocks.shape),
            "query_block_starts": self.query_block_starts.tolist(),
            "key_block_starts": self.key_block_starts.tolist(),
            "effective_block_size": self.effective_block_size,
            "block_content_hash": self.block_content_hash,
            "memory_estimate": self.memory_estimate,
            "block_score_max_abs_error": self.block_score_max_abs_error,
            "first_score_mismatch": self.first_score_mismatch,
            "code": self.code,
        }
        return meta, {
            "scores": self.scores,
            "blocks": self.blocks,
        }


def _block_starts(sequence_length: int, block_size: int) -> np.ndarray:
    return np.arange(0, sequence_length, block_size, dtype=np.int64)


def hash_block_content(
    q_rotated: np.ndarray,
    k_rotated: np.ndarray,
    block_size: int,
    causal: bool,
) -> str:
    seq_len = q_rotated.shape[1]
    starts = _block_starts(seq_len, min(block_size, seq_len))
    named: dict[str, np.ndarray] = {"q": q_rotated, "k": k_rotated}
    for block_index, start in enumerate(starts):
        stop = min(int(start) + block_size, seq_len)
        named[f"q_block_{block_index}"] = q_rotated[:, start:stop, :]
        named[f"k_block_{block_index}"] = k_rotated[:, start:stop, :]
    return sha256_json(
        {
            "block_size": block_size,
            "causal": causal,
            "block_array_hash": hash_arrays(named),
            "block_starts": starts.tolist(),
        }
    )


def compute_block_scores(
    q_rotated: np.ndarray,
    k_rotated: np.ndarray,
    block_size: int,
    causal: bool,
    tolerance: float = 1e-9,
) -> BlockScoreResult:
    heads, seq_len, head_dim = q_rotated.shape
    if k_rotated.shape != q_rotated.shape:
        raise ValueError("Q and K must have the same shape")
    effective_block_size = min(block_size, seq_len)
    starts = _block_starts(seq_len, effective_block_size)
    block_count = len(starts)
    scores = np.einsum("hqd,hkd->hqk", q_rotated, k_rotated) * (
        1.0 / np.sqrt(float(head_dim))
    )
    blocks = np.zeros(
        (
            heads,
            block_count,
            block_count,
            effective_block_size,
            effective_block_size,
        ),
        dtype=np.float64,
    )
    assembled = np.zeros((heads, seq_len, seq_len), dtype=np.float64)
    scale = 1.0 / np.sqrt(float(head_dim))
    for q_block_index, q_start in enumerate(starts):
        q_stop = min(int(q_start) + effective_block_size, seq_len)
        q_block = q_rotated[:, q_start:q_stop, :]
        for k_block_index, k_start in enumerate(starts):
            if causal and k_start > q_start:
                continue
            k_stop = min(int(k_start) + effective_block_size, seq_len)
            k_block = k_rotated[:, k_start:k_stop, :]
            block = np.einsum("hqd,hkd->hqk", q_block, k_block) * scale
            q_count = q_stop - int(q_start)
            k_count = k_stop - int(k_start)
            blocks[
                :,
                q_block_index,
                k_block_index,
                :q_count,
                :k_count,
            ] = block
            assembled[
                :,
                q_start:q_stop,
                k_start:k_stop,
            ] = block
    valid = np.ones((seq_len, seq_len), dtype=bool)
    if causal:
        valid = np.tril(valid)
    score_difference = np.abs(assembled - scores)
    valid_difference = score_difference[:, valid]
    score_error = (
        float(np.max(valid_difference)) if valid_difference.size else 0.0
    )
    first_score_mismatch = None
    if score_error > tolerance:
        masked_difference = np.where(valid[None, :, :], score_difference, 0.0)
        first_index = tuple(
            int(item) for item in np.argwhere(masked_difference > tolerance)[0]
        )
        first_score_mismatch = {
            "kind": "block_score_vs_full",
            "element_index": list(first_index),
            "head": first_index[0],
            "query_position": first_index[1],
            "key_position_or_dimension": first_index[2],
            "actual": float(assembled[first_index]),
            "expected": float(scores[first_index]),
            "deviation": float(score_difference[first_index]),
        }
    return BlockScoreResult(
        scores=scores,
        blocks=blocks,
        query_block_starts=starts.copy(),
        key_block_starts=starts.copy(),
        effective_block_size=effective_block_size,
        block_content_hash=hash_block_content(
            q_rotated, k_rotated, block_size, causal
        ),
        memory_estimate={
            "block_scores_bytes": int(blocks.nbytes),
            "assembled_scores_bytes": int(scores.nbytes),
        },
        block_score_max_abs_error=score_error,
        first_score_mismatch=first_score_mismatch,
        code="OK" if score_error <= tolerance else "E_BLOCK_MISMATCH",
    )


@dataclass(frozen=True)
class NormalizationResult:
    attention_output: np.ndarray
    row_max: np.ndarray
    row_sumexp: np.ndarray
    logsumexp: np.ndarray
    entropy: np.ndarray
    block_score_max_abs_error: float
    attention_output_max_abs_error: float
    normalization_max_abs_error: float
    first_mismatch: dict[str, Any] | None
    code: str
    equivalent: bool

    def to_artifact(self) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
        meta = {
            "block_score_max_abs_error": self.block_score_max_abs_error,
            "attention_output_max_abs_error": self.attention_output_max_abs_error,
            "normalization_max_abs_error": self.normalization_max_abs_error,
            "first_mismatch": self.first_mismatch,
            "code": self.code,
            "equivalent": self.equivalent,
        }
        return meta, {
            "attention_output": self.attention_output,
            "row_max": self.row_max,
            "row_sumexp": self.row_sumexp,
            "logsumexp": self.logsumexp,
            "entropy": self.entropy,
        }


def _first_abs_mismatch(
    actual: np.ndarray,
    expected: np.ndarray,
    tolerance: float,
    kind: str,
) -> dict[str, Any] | None:
    difference = np.abs(actual - expected)
    indices = np.argwhere(difference > tolerance)
    if indices.size == 0:
        return None
    index = tuple(int(item) for item in indices[0])
    return {
        "kind": kind,
        "element_index": list(index),
        "head": index[0] if len(index) > 0 else None,
        "query_position": index[1] if len(index) > 1 else None,
        "key_position_or_dimension": index[2] if len(index) > 2 else None,
        "actual": float(actual[index]),
        "expected": float(expected[index]),
        "deviation": float(difference[index]),
    }


def _full_attention(
    scores: np.ndarray,
    values: np.ndarray,
    causal: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    heads, seq_len, _ = scores.shape
    valid = np.ones((seq_len, seq_len), dtype=bool)
    if causal:
        valid = np.tril(valid)
    masked_scores = np.where(valid[None, :, :], scores, -np.inf)
    row_max = np.max(masked_scores, axis=-1)
    shifted = np.exp(masked_scores - row_max[..., None])
    row_sumexp = np.sum(shifted, axis=-1)
    probabilities = shifted / row_sumexp[..., None]
    output = np.einsum("hqk,hkd->hqd", probabilities, values)
    logsumexp = row_max + np.log(row_sumexp)
    safe_probabilities = np.where(probabilities > 0.0, probabilities, 1.0)
    entropy = -np.sum(
        np.where(
            probabilities > 0.0,
            probabilities * np.log(safe_probabilities),
            0.0,
        ),
        axis=-1,
    )
    return output, row_max, row_sumexp, logsumexp, entropy, masked_scores


def compute_online_normalization(
    block_scores: BlockScoreResult,
    values: np.ndarray,
    causal: bool,
    tolerance: float = 1e-9,
) -> NormalizationResult:
    heads, seq_len, head_dim = values.shape
    block_size = block_scores.effective_block_size
    starts = block_scores.query_block_starts
    block_count = len(starts)

    output = np.zeros((heads, seq_len, head_dim), dtype=np.float64)
    row_max = np.zeros((heads, seq_len), dtype=np.float64)
    row_sumexp = np.zeros((heads, seq_len), dtype=np.float64)

    for q_block_index, q_start in enumerate(starts):
        q_stop = min(int(q_start) + block_size, seq_len)
        q_count = q_stop - int(q_start)
        running_max = np.full((heads, q_count), -np.inf, dtype=np.float64)
        running_sum = np.zeros((heads, q_count), dtype=np.float64)
        running_weighted = np.zeros((heads, q_count, head_dim), dtype=np.float64)
        for k_block_index, k_start in enumerate(starts):
            if causal and k_start > q_start:
                break
            k_stop = min(int(k_start) + block_size, seq_len)
            k_count = k_stop - int(k_start)
            block = block_scores.blocks[
                :,
                q_block_index,
                k_block_index,
                :q_count,
                :k_count,
            ]
            q_positions = np.arange(int(q_start), q_stop)[:, None]
            k_positions = np.arange(int(k_start), k_stop)[None, :]
            valid = np.ones((q_count, k_count), dtype=bool)
            if causal:
                valid &= k_positions <= q_positions
            masked = np.where(valid[None, :, :], block, -np.inf)
            block_max = np.max(masked, axis=-1)
            new_max = np.maximum(running_max, block_max)
            correction = np.exp(running_max - new_max)
            probabilities = np.exp(masked - new_max[..., None])
            running_sum = running_sum * correction + np.sum(probabilities, axis=-1)
            values_block = values[:, k_start:k_stop, :]
            running_weighted = (
                running_weighted * correction[..., None]
                + np.einsum("hqk,hkd->hqd", probabilities, values_block)
            )
            running_max = new_max
        output[:, q_start:q_stop, :] = running_weighted / running_sum[..., None]
        row_max[:, q_start:q_stop] = running_max
        row_sumexp[:, q_start:q_stop] = running_sum

    logsumexp = row_max + np.log(row_sumexp)
    valid = np.ones((seq_len, seq_len), dtype=bool)
    if causal:
        valid = np.tril(valid)
    masked_scores = np.where(
        valid[None, :, :], block_scores.scores, -np.inf
    )
    probabilities = np.exp(masked_scores - logsumexp[:, :, None])
    safe_probabilities = np.where(probabilities > 0.0, probabilities, 1.0)
    entropy = -np.sum(
        np.where(
            probabilities > 0.0,
            probabilities * np.log(safe_probabilities),
            0.0,
        ),
        axis=-1,
    )

    # The assembled block matrix is compared to the independent full matrix
    # in compute_block_scores; here the full path is normalized from the full
    # matrix while the block path uses only resident key blocks.
    (
        full_output,
        full_max,
        full_sumexp,
        full_logsumexp,
        full_entropy,
        masked_scores,
    ) = _full_attention(block_scores.scores, values, causal)

    block_score_error = block_scores.block_score_max_abs_error
    output_error = float(np.max(np.abs(output - full_output)))
    normalization_error = max(
        float(np.max(np.abs(row_max - full_max))),
        float(np.max(np.abs(row_sumexp - full_sumexp))),
        float(np.max(np.abs(logsumexp - full_logsumexp))),
    )
    first_mismatch = block_scores.first_score_mismatch
    if first_mismatch is None:
        first_mismatch = _first_abs_mismatch(
            output, full_output, tolerance, "attention_output_vs_full"
        )
    if first_mismatch is None:
        first_mismatch = _first_abs_mismatch(
            row_sumexp, full_sumexp, tolerance, "normalization_stats_vs_full"
        )
    equivalent = (
        block_score_error <= tolerance
        and output_error <= tolerance
        and normalization_error <= tolerance
    )
    return NormalizationResult(
        attention_output=output,
        row_max=row_max,
        row_sumexp=row_sumexp,
        logsumexp=logsumexp,
        entropy=entropy,
        block_score_max_abs_error=block_score_error,
        attention_output_max_abs_error=output_error,
        normalization_max_abs_error=normalization_error,
        first_mismatch=first_mismatch,
        code="OK" if equivalent else "E_BLOCK_MISMATCH",
        equivalent=equivalent,
    )


def _valid_score_mask(seq_len: int, causal: bool) -> np.ndarray:
    mask = np.ones((seq_len, seq_len), dtype=bool)
    if causal:
        mask = np.tril(mask)
    return mask


def compare_variant_scores(
    source_scores: np.ndarray,
    target_scores: np.ndarray,
    causal: bool,
    tolerance: float,
) -> dict[str, Any]:
    if source_scores.shape != target_scores.shape:
        raise ValueError("variant score matrices must have the same shape")
    seq_len = source_scores.shape[-1]
    mask = _valid_score_mask(seq_len, causal)
    difference = np.abs(source_scores - target_scores)
    valid_difference = difference[:, mask]
    max_deviation = float(np.max(valid_difference)) if valid_difference.size else 0.0
    return {
        "max_abs_deviation": max_deviation,
        "equivalent": max_deviation <= tolerance,
        "tolerance": tolerance,
        "ranking_consistency_ratio": ranking_consistency_ratio(
            source_scores, target_scores, causal
        ),
        "valid_cell_count": int(valid_difference.size),
    }


def ranking_consistency_ratio(
    source_scores: np.ndarray,
    target_scores: np.ndarray,
    causal: bool,
) -> float:
    """Pairwise order agreement over every valid query row."""

    heads, seq_len, _ = source_scores.shape
    agreements = 0
    total = 0
    for head in range(heads):
        for query in range(seq_len):
            valid_keys = query + 1 if causal else seq_len
            source_row = source_scores[head, query, :valid_keys]
            target_row = target_scores[head, query, :valid_keys]
            for left in range(valid_keys):
                for right in range(left + 1, valid_keys):
                    source_sign = int(np.sign(source_row[left] - source_row[right]))
                    target_sign = int(np.sign(target_row[left] - target_row[right]))
                    total += 1
                    if source_sign == target_sign:
                        agreements += 1
    if total == 0:
        return 1.0
    return agreements / float(total)
