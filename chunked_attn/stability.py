"""Numerical stability verification (requirement 5).

Per-KV-block statistics (m_blk, l_blk) must merge back into the whole-
segment softmax denominator:

    denom[row] = sum_b exp(m_blk[b][row] - m_global[row]) * l_blk[b][row]
    m_global[row] = max_b m_blk[b][row]

If the reconstruction deviates from the whole-segment denominator beyond
the declared tolerance, E_STABILITY_VIOLATION is raised with the block
position of the maximum deviation.
"""
from __future__ import annotations

import numpy as np

from .errors import BizError, E_STABILITY_VIOLATION


def reconstruct_denominator(kv_stats):
    """Merge per-KV-block stats into the softmax denominator (float64)."""
    m_global = np.full_like(kv_stats[0].m_blk, -np.inf)
    for st in kv_stats:
        m_global = np.maximum(m_global, st.m_blk)
    denom = np.zeros_like(m_global)
    for st in kv_stats:
        with np.errstate(invalid="ignore"):
            w = np.exp(st.m_blk - m_global)
        w = np.where(np.isfinite(st.m_blk), w, 0.0)
        denom = denom + w * st.l_blk
    return denom, m_global


def verify_run(chunked_run, whole_run, cfg, case_id=None):
    """Verify every Q block/head: reconstructed denominator must match the
    whole-segment denominator within cfg.tolerance."""
    worst = {"deviation": 0.0}
    for head in range(chunked_run.n_heads):
        whole_blocks = whole_run.q_blocks[head]
        whole_denom = whole_blocks[0].denom
        for qb in chunked_run.q_blocks[head]:
            recon, _ = reconstruct_denominator(qb.kv_stats)
            ref = whole_denom[qb.q_start:qb.q_end]
            dev = float(np.max(np.abs(recon - ref))) if recon.size else 0.0
            if dev > worst["deviation"]:
                worst = {
                    "deviation": dev,
                    "head": head,
                    "q_block_idx": qb.q_block_idx,
                    "q_start": qb.q_start,
                    "q_end": qb.q_end,
                }
            if dev > cfg.tolerance:
                raise BizError(
                    E_STABILITY_VIOLATION,
                    "denominator reconstruction deviation %.3e exceeds "
                    "tolerance %.3e" % (dev, cfg.tolerance),
                    case_id=case_id, stage="stability",
                    details={
                        "head": head,
                        "q_block_idx": qb.q_block_idx,
                        "q_start": qb.q_start,
                        "q_end": qb.q_end,
                        "deviation": dev,
                        "tolerance": cfg.tolerance,
                    })
    return {"verdict": "OK",
            "max_reconstruction_deviation": worst["deviation"],
            "tolerance": cfg.tolerance}
