"""分块注意力打分、在线 softmax 归一化与整段对照。

数据布局统一为 ``(seq_len, num_heads, head_dim)``。缩放点积口径：
``score = Q @ K^T / sqrt(head_dim)``。causal 下查询位置 p 只能访问
键位置 k <= p，未来位置以 -inf 掩码后不参与 softmax。

在线 softmax（FlashAttention 风格）逐块维护 float64 统计：
    m      当前全局最大值；
    d      当前全局归一化分母（以 m 平移后的指数和）；
    acc    以当前 m 平移的加权输出累加器。
每个分块单独保存其块内最大值与块内分母，可按公式合并还原整段分母。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator, List, Tuple

import numpy as np

from .config import RunConfig, Task
from .numeric import quantize


def block_ranges(seq_len: int, block_size: int) -> List[Tuple[int, int]]:
    return [
        (start, min(start + block_size, seq_len))
        for start in range(0, seq_len, block_size)
    ]


def scaled_dot_product(
    query: np.ndarray, key: np.ndarray
) -> np.ndarray:
    head_dim = query.shape[-1]
    scale = 1.0 / math.sqrt(head_dim)
    # (q_len, heads, dim) x (k_len, heads, dim) -> (q_len, k_len, heads)
    scores = np.einsum("qhd,khd->qkh", query, key) * scale
    return scores.astype(np.float64, copy=False)


def apply_causal_mask(
    scores: np.ndarray, q_offset: int, k_offset: int
) -> np.ndarray:
    q_len, k_len = scores.shape[0], scores.shape[1]
    q_positions = np.arange(q_offset, q_offset + q_len)[:, None, None]
    k_positions = np.arange(k_offset, k_offset + k_len)[None, :, None]
    masked = np.where(k_positions <= q_positions, scores, -np.inf)
    return masked


@dataclass
class BlockStats:
    """单个键分块的可合并统计量（float64 口径）。"""

    block_index: int
    q_start: int
    q_end: int
    k_start: int
    k_end: int
    block_max: np.ndarray  # (q_len, heads)
    block_denom: np.ndarray  # (q_len, heads)，块内 exp 平移和


def compute_block_stats(
    task: Task,
    query: np.ndarray,
    key: np.ndarray,
    block_index: int,
    q_range: Tuple[int, int],
    k_range: Tuple[int, int],
    config: RunConfig,
) -> BlockStats:
    q_start, q_end = q_range
    k_start, k_end = k_range
    scores = scaled_dot_product(query[q_start:q_end], key[k_start:k_end])
    if task.causal:
        scores = apply_causal_mask(scores, q_start, k_start)
    block_max = np.max(scores, axis=1)  # (q_len, heads)
    # causal 下整块被掩码的行 max 为 -inf：保留 -inf，指数项 exp(-inf)=0，
    # 该块对分母与输出的贡献自然归零，合并时仍可正确还原整段分母。
    safe_max = np.where(np.isfinite(block_max), block_max, 0.0)
    shifted = scores - safe_max[:, np.newaxis, :]
    shifted = np.where(np.isfinite(scores), shifted, -np.inf)
    block_denom = np.sum(np.exp(shifted), axis=1)  # (q_len, heads)
    block_max_q = quantize(
        block_max,
        config.stat_dtype,
        config.rounding,
        config.subnormal_protection,
    )
    block_denom_q = quantize(
        block_denom,
        config.stat_dtype,
        config.rounding,
        config.subnormal_protection,
    )
    return BlockStats(
        block_index=block_index,
        q_start=q_start,
        q_end=q_end,
        k_start=k_start,
        k_end=k_end,
        block_max=np.asarray(block_max_q, dtype=np.float64),
        block_denom=np.asarray(block_denom_q, dtype=np.float64),
    )


@dataclass
class OnlineState:
    running_max: np.ndarray
    running_denom: np.ndarray
    output: np.ndarray


def init_online_state(shape: Tuple[int, int], head_dim: int) -> OnlineState:
    return OnlineState(
        running_max=np.full(shape, -np.inf, dtype=np.float64),
        running_denom=np.zeros(shape, dtype=np.float64),
        output=np.zeros((*shape, head_dim), dtype=np.float64),
    )


def update_online_state(
    state: OnlineState,
    stats: BlockStats,
    value: np.ndarray,
    scores: np.ndarray,
) -> OnlineState:
    """消费一个键分块，更新最大值、分母与加权输出。"""
    new_max = np.maximum(state.running_max, stats.block_max)
    # 旧统计量按新最大值重新缩放；-inf 初始值对应缩放因子 0。
    old_scale = np.exp(
        np.where(np.isfinite(state.running_max), state.running_max - new_max, 0.0)
    )
    block_scale = np.exp(stats.block_max - new_max)
    new_denom = state.running_denom * old_scale + stats.block_denom * block_scale

    shifted_scores = scores - new_max[:, None, :]
    prob = np.exp(
        np.where(np.isfinite(shifted_scores), shifted_scores, -np.inf)
    )
    block_values = value[stats.k_start:stats.k_end]
    block_out = np.einsum("qkh,khd->qhd", prob, block_values)
    new_output = state.output * old_scale[:, :, None] + block_out
    return OnlineState(
        running_max=new_max,
        running_denom=new_denom,
        output=new_output,
    )


def finalize_output(state: OnlineState, dtype: str) -> np.ndarray:
    denominator = state.running_denom[:, :, None]
    normalized = state.output / denominator
    return np.ascontiguousarray(normalized.astype(dtype))


def merged_full_denom(
    block_stats: List[BlockStats],
) -> Tuple[np.ndarray, np.ndarray]:
    """由各分块统计量合并还原整段 softmax 分母与全局最大值。

    合并公式：给定块 (m_b, d_b)，全局
        M = max_b m_b
        D = sum_b d_b * exp(m_b - M)
    """
    global_max = block_stats[0].block_max.copy()
    for stats in block_stats[1:]:
        global_max = np.maximum(global_max, stats.block_max)
    denom = np.zeros_like(global_max, dtype=np.float64)
    for stats in block_stats:
        denom += stats.block_denom * np.exp(stats.block_max - global_max)
    return global_max, denom


def full_reference(
    task: Task,
    query: np.ndarray,
    key: np.ndarray,
    value: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """不分块整段参考路径（float64），返回输出、全局最大值与整段分母。"""
    scores = scaled_dot_product(query, key)
    if task.causal:
        scores = apply_causal_mask(scores, 0, 0)
    global_max = np.max(scores, axis=1)
    safe_max = np.where(np.isfinite(global_max), global_max, 0.0)
    shifted = scores - safe_max[:, None, :]
    probabilities = np.exp(
        np.where(np.isfinite(shifted), shifted, -np.inf)
    )
    denom = np.sum(probabilities, axis=1)  # (q, heads)
    output = (
        np.einsum("qkh,khd->qhd", probabilities, value)
        / denom[:, :, None]
    )
    return (
        np.ascontiguousarray(output.astype(task.dtype)),
        global_max,
        denom.astype(np.float64),
    )


def elementwise_deviation(
    chunked: np.ndarray, reference: np.ndarray
) -> dict:
    """逐元素最大绝对/相对偏差，明细可独立重算（仅依赖两个输出数组）。"""
    chunked64 = chunked.astype(np.float64)
    reference64 = reference.astype(np.float64)
    abs_diff = np.abs(chunked64 - reference64)
    denom = np.maximum(np.abs(reference64), 1e-12)
    rel_diff = abs_diff / denom
    index = np.unravel_index(np.argmax(abs_diff), abs_diff.shape)
    return {
        "max_abs_diff": float(np.max(abs_diff)),
        "max_rel_diff": float(np.max(rel_diff)),
        "max_abs_index": list(int(axis) for axis in index),
        "mean_abs_diff": float(np.mean(abs_diff)),
    }


def denominator_restore_error(
    restored: np.ndarray, reference: np.ndarray
) -> np.ndarray:
    """整段分母还原的逐元素相对偏差。"""
    safe_ref = np.where(np.abs(reference) > 0, reference, 1.0)
    return np.abs(restored - reference) / np.abs(safe_ref)


def iter_query_blocks(
    seq_len: int, block_size: int
) -> Iterator[Tuple[int, int]]:
    yield from block_ranges(seq_len, block_size)
