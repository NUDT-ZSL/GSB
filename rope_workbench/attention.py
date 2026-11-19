"""分块注意力打分与在线归一化。

阶段划分：
  score 阶段    —— 按 block_size 切分查询块，逐键块计算打分并施加 causal 掩码
  normalize 阶段 —— 在线 softmax（流式 max/sum 归一化）累计注意力输出

所有中间量均为 float64。causal 语义下未来位置打分置 -inf，禁止参与归一化。
"""

from __future__ import annotations

import numpy as np

NEG_INF = float("-inf")


def causal_mask(q_lo, q_hi, k_lo, k_hi):
    """返回 (q_hi-q_lo, k_hi-k_lo) 布尔掩码，True 表示允许访问（k_pos <= q_pos）。"""
    q_pos = np.arange(q_lo, q_hi)[:, None]
    k_pos = np.arange(k_lo, k_hi)[None, :]
    return k_pos <= q_pos


def score_blocks(q, k, block_size, causal):
    """score 阶段：返回 [(q_lo, q_hi, k_lo, k_hi, scores)]，scores 形状 (H, qb, kb)。

    causal 下被掩蔽位置打分置 -inf。打分 = q · k / sqrt(head_dim)。
    """
    h, s, d = q.shape
    scale = 1.0 / np.sqrt(np.float64(d))
    blocks = []
    for q_lo in range(0, s, block_size):
        q_hi = min(q_lo + block_size, s)
        for k_lo in range(0, s, block_size):
            k_hi = min(k_lo + block_size, s)
            if causal and k_lo > q_hi - 1:
                continue  # 整块都在未来，禁止访问
            sc = (q[:, q_lo:q_hi, :] @ k[:, k_lo:k_hi, :].transpose(0, 2, 1)) * scale
            if causal:
                mask = causal_mask(q_lo, q_hi, k_lo, k_hi)
                sc = np.where(mask[None, :, :], sc, NEG_INF)
            blocks.append((q_lo, q_hi, k_lo, k_hi, sc))
    return blocks


def _uniform_block(recs, v, h, qb, d):
    """归一化降级（首块）：允许位置上的均匀权重输出。"""
    acc = np.zeros((h, qb, d), dtype=np.float64)
    cnt = np.zeros((h, qb), dtype=np.float64)
    for (_, _, k_lo, k_hi, sc) in recs:
        allowed = np.isfinite(sc)
        weights = np.where(allowed[..., None], 1.0, 0.0)
        v_blk = np.broadcast_to(v[:, None, k_lo:k_hi, :],
                                (h, qb, k_hi - k_lo, d))
        acc += (weights * v_blk).sum(axis=2)
        cnt += allowed.sum(axis=2)
    cnt = np.maximum(cnt, 1.0)
    return acc / cnt[:, :, None], cnt


def normalize_blocks(blocks, v, seq_len, degrade_prev_stats=False):
    """normalize 阶段：在线 softmax 累计，返回 (output, stats)。

    output: (H, S, D) float64；stats 记录每个查询块使用的归一化模式。
    degrade_prev_stats=True 时（归一化降级）：查询块沿用上一查询块的
    (m, l) 统计量不再更新；首个查询块退化为允许位置的均匀权重。
    """
    h, s, d = v.shape
    out = np.zeros((h, s, d), dtype=np.float64)
    stats = []
    q_groups = []
    for rec in blocks:
        if not q_groups or q_groups[-1][0] != rec[0]:
            q_groups.append((rec[0], rec[1], []))
        q_groups[-1][2].append(rec)

    prev_m = prev_l = None
    for q_lo, q_hi, recs in q_groups:
        qb = q_hi - q_lo
        if degrade_prev_stats:
            if prev_m is None:
                blk_out, cnt = _uniform_block(recs, v, h, qb, d)
                out[:, q_lo:q_hi, :] = blk_out
                prev_m = np.zeros((h, qb), dtype=np.float64)
                prev_l = cnt.copy()
                stats.append({"q_lo": q_lo, "q_hi": q_hi, "mode": "uniform_weights"})
            else:
                acc = np.zeros((h, qb, d), dtype=np.float64)
                for (_, _, k_lo, k_hi, sc) in recs:
                    p = np.exp(sc - prev_m[:, :, None])
                    acc += p @ v[:, k_lo:k_hi, :]
                out[:, q_lo:q_hi, :] = acc / prev_l[:, :, None]
                stats.append({"q_lo": q_lo, "q_hi": q_hi, "mode": "reuse_prev_stats"})
            continue

        m = np.full((h, qb), NEG_INF, dtype=np.float64)
        l = np.zeros((h, qb), dtype=np.float64)
        acc = np.zeros((h, qb, d), dtype=np.float64)
        for (_, _, k_lo, k_hi, sc) in recs:
            blk_max = sc.max(axis=2)
            m_new = np.maximum(m, blk_max)
            safe_m = np.where(np.isfinite(m_new), m_new, 0.0)
            corr = np.exp(np.where(np.isfinite(m), m - safe_m, NEG_INF))
            p = np.exp(sc - safe_m[:, :, None])
            l = l * corr + p.sum(axis=2)
            acc = acc * corr[:, :, None] + p @ v[:, k_lo:k_hi, :]
            m = m_new
        l_safe = np.where(l > 0.0, l, 1.0)
        out[:, q_lo:q_hi, :] = acc / l_safe[:, :, None]
        stats.append({"q_lo": q_lo, "q_hi": q_hi, "mode": "online_softmax"})
        prev_m, prev_l = m, l
    return out, stats


def full_attention(q, k, v, causal):
    """整段参考路径：单块在线 softmax，与分块路径对照。"""
    s = q.shape[1]
    blocks = score_blocks(q, k, s, causal)
    out, _ = normalize_blocks(blocks, v, s)
    return out
