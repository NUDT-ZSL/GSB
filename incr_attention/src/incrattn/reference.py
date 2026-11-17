"""Serial float64 reference attention and precision-attribution variants."""

from __future__ import annotations

import numpy as np

from .kernel import BlockResult
from .util import block_span, num_blocks


def _softmax_components(queries, keys, start, end, visible_end, block_size, scale):
    """Serial per-row float64 QK scores, causal mask and softmax stats."""
    q_block = queries[start:end].astype(np.float64)
    rows = end - start
    scores = np.zeros((rows, visible_end), dtype=np.float64)
    for row in range(rows):
        query = q_block[row]
        query_index = start + row
        for key_index in range(visible_end):
            value = 0.0
            for channel in range(query.shape[0]):
                value += float(query[channel]) * float(keys[key_index, channel])
            scores[row, key_index] = value * float(scale)
            if key_index > query_index:
                scores[row, key_index] = -np.inf
    row_max = np.max(scores, axis=1)
    exp_scores = np.exp(scores - row_max[:, np.newaxis])
    exp_sum = np.sum(exp_scores, axis=1)
    return scores, row_max, exp_scores, exp_sum


def reference_block(
    block_index,
    queries,
    keys,
    values,
    block_size,
    scale,
    variant="full",
):
    """Float64 serial reference for one output block.

    variant:
      full       - every stage in float64 (ground truth).
      max32      - row maximum selected in float32, everything else float64.
      sum32      - max and exponential-sum accumulation rounded to float32.
      weighted32 - softmax probabilities rounded to float32 before the V sum.
    """
    queries64 = np.asarray(queries, dtype=np.float64)
    keys64 = np.asarray(keys, dtype=np.float64)
    values64 = np.asarray(values, dtype=np.float64)
    n_q = queries64.shape[0]
    n_kv = keys64.shape[0]
    head_dim = queries64.shape[1]
    start, end = block_span(n_q, block_size, block_index)
    visible_end = min(n_kv, end)

    scores, row_max, exp_scores, exp_sum = _softmax_components(
        queries64, keys64, start, end, visible_end, block_size, scale
    )
    if variant == "max32":
        row_max = row_max.astype(np.float32).astype(np.float64)
        exp_scores = np.exp(scores - row_max[:, np.newaxis])
        exp_sum = np.sum(exp_scores, axis=1)
    elif variant == "sum32":
        row_max = row_max.astype(np.float32).astype(np.float64)
        exp_scores = np.exp(scores - row_max[:, np.newaxis]).astype(
            np.float32
        ).astype(np.float64)
        exp_sum = np.sum(exp_scores, axis=1).astype(np.float32).astype(np.float64)
    elif variant == "weighted32":
        pass
    elif variant != "full":
        raise ValueError(f"unknown reference variant {variant}")

    probabilities = exp_scores / exp_sum[:, np.newaxis]
    if variant == "weighted32":
        probabilities = probabilities.astype(np.float32).astype(np.float64)

    rows = end - start
    output = np.zeros((rows, head_dim), dtype=np.float64)
    for row in range(rows):
        for key_index in range(visible_end):
            weight = float(probabilities[row, key_index])
            for channel in range(head_dim):
                output[row, channel] += (
                    weight * float(values64[key_index, channel])
                )

    output32 = output.astype(np.float32)
    row_max32 = row_max.astype(np.float32)
    exp_sum32 = exp_sum.astype(np.float32)
    last_kv_block = num_blocks(visible_end, block_size) - 1
    from .kernel import digest_block

    return BlockResult(
        block_index=block_index,
        row_max=row_max32,
        exp_sum=exp_sum32,
        output=output32,
        kv_block_range=(0, last_kv_block + 1),
        fingerprint=digest_block(
            block_index,
            row_max32,
            exp_sum32,
            output32,
            (0, last_kv_block + 1),
        ),
    )


def reference_full(queries, keys, values, block_size, scale, variant="full"):
    """One-shot full recompute of every output block, float64 serial path."""
    n_q = queries.shape[0]
    return [
        reference_block(
            index, queries, keys, values, block_size, scale, variant=variant
        )
        for index in range(num_blocks(n_q, block_size))
    ]
