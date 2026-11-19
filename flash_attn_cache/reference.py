from __future__ import annotations

import math

import numpy as np

from .models import AttentionCase, QKVArrays


def dense_attention_reference(
    case: AttentionCase,
    arrays: QKVArrays,
) -> np.ndarray:
    """Full-sequence float64 reference with the same mask and scale."""

    q = np.asarray(arrays.q, dtype=np.float64)
    k = np.asarray(arrays.k, dtype=np.float64)
    v = np.asarray(arrays.v, dtype=np.float64)
    scale = (
        case.scale
        if case.scale is not None
        else 1.0 / math.sqrt(float(case.head_dim))
    )
    scores = np.matmul(q, np.swapaxes(k, -1, -2)) * scale
    if case.causal:
        positions = np.arange(case.seq_len)
        mask = positions[None, :] <= positions[:, None]
        scores = np.where(mask[None, :, :], scores, -np.inf)
    row_max = np.max(scores, axis=-1, keepdims=True)
    weights = np.exp(scores - row_max)
    denominator = np.sum(weights, axis=-1, keepdims=True)
    return np.ascontiguousarray(np.matmul(weights, v) / denominator)
