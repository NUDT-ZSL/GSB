"""FlashAttention-style chunked online attention kernel (NumPy, float32).

The kernel streams K/V blocks and maintains the numerically stable online
softmax state (running row maximum and running rescaled exponential sum).
All accumulation happens in float64; observable block outputs are float32.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import (
    KernelStageError,
    LengthMismatchError,
    NonFiniteInputError,
)
from .util import array_fingerprint, block_span, num_blocks


@dataclass
class BlockResult:
    """Observable result of one output block."""

    block_index: int
    row_max: np.ndarray          # float32, shape (rows,)
    exp_sum: np.ndarray          # float32, shape (rows,)
    output: np.ndarray           # float32, shape (rows, channels)
    kv_block_range: tuple[int, int]
    fingerprint: str


def _finite_check(name, array):
    if not np.all(np.isfinite(array)):
        raise NonFiniteInputError(f"non-finite element detected in {name}")


def attention_block(
    block_index,
    queries,
    keys,
    values,
    block_size,
    scale,
    injector=None,
):
    """Compute one output block over all currently visible K/V blocks.

    queries: full Q array (n_q, d) or the block slice; row span is derived
             deterministically from ``block_index`` and ``block_size``.
    keys/values: full current K/V arrays (n_kv, d).
    The K/V block iteration order and the inner (query row, key position,
    channel) accumulation order are fixed, so repeated runs are bit-identical.
    """
    queries = np.asarray(queries, dtype=np.float32)
    keys = np.asarray(keys, dtype=np.float32)
    values = np.asarray(values, dtype=np.float32)

    if queries.ndim != 2 or keys.ndim != 2 or values.ndim != 2:
        raise LengthMismatchError("Q/K/V must be rank-2 arrays")
    if keys.shape != values.shape:
        raise LengthMismatchError(
            f"K/V shape mismatch {keys.shape} vs {values.shape}"
        )
    if queries.shape[1] != keys.shape[1]:
        raise LengthMismatchError("Q and K head dimensions differ")

    _finite_check("Q", queries)
    _finite_check("K", keys)
    _finite_check("V", values)

    n_q, head_dim = queries.shape
    n_kv = keys.shape[0]
    start, end = block_span(n_q, block_size, block_index)
    if start >= n_q:
        raise LengthMismatchError(
            f"query block {block_index} outside query length {n_q}"
        )

    q_block = queries[start:end]
    rows = end - start
    visible_end = min(n_kv, end)
    last_kv_block = num_blocks(visible_end, block_size) - 1
    kv_block_range = (0, last_kv_block + 1)

    neg_inf = np.float32(-np.inf)
    running_max = np.full(rows, neg_inf, dtype=np.float64)
    running_sum = np.zeros(rows, dtype=np.float64)
    accumulator = np.zeros((rows, head_dim), dtype=np.float64)

    if injector is not None:
        injector.on_stage("score", block_index)

    for kv_index in range(last_kv_block + 1):
        kv_start, kv_stop = block_span(n_kv, block_size, kv_index)
        k_block = keys[kv_start:kv_stop]
        v_block = values[kv_start:kv_stop]

        # Fixed inner counting order: query rows, then key positions, then
        # channels. matmul produces the same Q*K^T values regardless of
        # thread scheduling; downstream loops are explicitly row ordered.
        scores = np.matmul(q_block, k_block.T, dtype=np.float32) * np.float32(scale)
        causal_columns = np.arange(kv_start, kv_stop)
        # Causal rule: query at position i may attend key positions j <= i.
        causal_mask = causal_columns[np.newaxis, :] <= np.arange(start, end)[:, np.newaxis]
        scores = np.where(causal_mask, scores, np.float32(-np.inf))

        if injector is not None:
            injector.on_stage("normalize", block_index)

        block_max = np.max(scores, axis=1).astype(np.float64)
        block_max = np.where(np.isfinite(block_max), block_max, 0.0)
        new_max = np.maximum(running_max, block_max)
        exp_scores = np.exp(
            (scores.astype(np.float64) - new_max[:, np.newaxis])
        )
        exp_scores = np.where(causal_mask, exp_scores, 0.0)
        block_sum = np.sum(exp_scores, axis=1)

        if injector is not None:
            injector.on_stage("weighted", block_index)

        rescale = np.exp(running_max - new_max)
        running_sum = running_sum * rescale + block_sum
        accumulator = accumulator * rescale[:, np.newaxis]
        for row in range(rows):
            for key_pos in range(kv_stop - kv_start):
                weight = float(exp_scores[row, key_pos])
                for channel in range(head_dim):
                    accumulator[row, channel] += (
                        weight * float(v_block[key_pos, channel])
                    )
        running_max = new_max

    if running_sum.shape[0] and np.any(running_sum == 0.0):
        raise KernelStageError(
            "normalize", "zero exponential sum: no visible key for a query row"
        )

    output = (accumulator / running_sum[:, np.newaxis]).astype(np.float32)
    row_max = running_max.astype(np.float32)
    exp_sum = running_sum.astype(np.float32)

    fingerprint = digest_block(
        block_index,
        row_max,
        exp_sum,
        output,
        kv_block_range,
    )
    return BlockResult(
        block_index=block_index,
        row_max=row_max,
        exp_sum=exp_sum,
        output=output,
        kv_block_range=kv_block_range,
        fingerprint=fingerprint,
    )


def digest_block(block_index, row_max, exp_sum, output, kv_block_range):
    return array_fingerprint(
        np.concatenate(
            [
                np.asarray([block_index], dtype=np.int64).view(np.uint8),
                row_max.astype(np.float32, copy=False).view(np.uint8).reshape(-1),
                exp_sum.astype(np.float32, copy=False).view(np.uint8).reshape(-1),
                output.astype(np.float32, copy=False).view(np.uint8).reshape(-1),
                np.asarray(kv_block_range, dtype=np.int64).view(np.uint8),
            ]
        )
    )
