"""Deterministic two-level blocked attention kernel (requirements 1, 3, 5).

Design notes for byte-exact reproducibility:

* All intermediate statistics (block maxima, normalisation denominators and
  the output accumulator) are computed in float64.
* Score and PV reductions use broadcast-multiply followed by ``np.sum`` over
  a fixed-length axis, so the reduction order depends only on the reduced
  axis length -- never on the Q blocking. As long as ``kv_block_len`` is
  identical, a run with ``q_block_len == seq_len`` (whole-segment) therefore
  reproduces the chunked run bit for bit.
* The online-softmax merge order is KV blocks in ascending index order; the
  whole-segment pass uses the exact same KV merge sequence, which is what
  makes the merge convention at block boundaries identical by construction.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np

from .config import ROUND_NEAREST_EVEN, ROUND_TOWARD_ZERO, StabilityConfig

_F64 = np.float64
_TINY = np.finfo(_F64).tiny
_NEG_INF = float("-inf")


def _flush_subnormal(x: np.ndarray, cfg: StabilityConfig) -> np.ndarray:
    if cfg.subnormal_protection:
        return np.where(np.abs(x) < _TINY, 0.0, x)
    return x


def scores_block(q_blk: np.ndarray, k_blk: np.ndarray, scale: float) -> np.ndarray:
    """(bq, d) x (bk, d) -> (bq, bk) float64, deterministic reduction order."""
    prod = q_blk[:, None, :] * k_blk[None, :, :]
    return prod.sum(axis=-1, dtype=_F64) * scale


def pv_block(p: np.ndarray, v_blk: np.ndarray) -> np.ndarray:
    """(bq, bk) x (bk, dv) -> (bq, dv) float64, deterministic reduction order."""
    prod = p[:, :, None] * v_blk[None, :, :]
    return prod.sum(axis=1, dtype=_F64)


@dataclass
class KVBlockStat:
    """Online-softmax statistics of one KV block for one Q block/head."""

    kv_block_idx: int
    kv_start: int
    kv_end: int
    m_blk: np.ndarray  # (bq,) row max of the (masked) scores
    l_blk: np.ndarray  # (bq,) rowsum of exp(score - m_blk)

    def checksum(self) -> str:
        h = hashlib.sha256()
        h.update(np.ascontiguousarray(self.m_blk).tobytes())
        h.update(np.ascontiguousarray(self.l_blk).tobytes())
        return h.hexdigest()


@dataclass
class QBlockResult:
    q_block_idx: int
    q_start: int
    q_end: int
    output: np.ndarray          # (bq, dv) float64
    m: np.ndarray               # (bq,) merged running max
    denom: np.ndarray           # (bq,) merged softmax denominator
    kv_stats: list = field(default_factory=list)


@dataclass
class ChunkedRun:
    """Result of a full chunked (or whole-segment) attention pass."""

    n_heads: int
    seq_len: int
    head_dim: int
    q_block_len: int
    kv_block_len: int
    causal: bool
    # q_blocks[head][q_block_idx] -> QBlockResult
    q_blocks: list
    output: np.ndarray          # (n_heads, seq_len, dv) float64

    def output_checksum(self) -> str:
        return hashlib.sha256(np.ascontiguousarray(self.output).tobytes()).hexdigest()

    def stats_checksum(self) -> str:
        h = hashlib.sha256()
        for head_blocks in self.q_blocks:
            for qb in head_blocks:
                h.update(np.ascontiguousarray(qb.m).tobytes())
                h.update(np.ascontiguousarray(qb.denom).tobytes())
                for st in qb.kv_stats:
                    h.update(st.checksum().encode())
        return h.hexdigest()


def attention_online(q_blk, k, v, q_start, causal, kv_block_len, scale,
                     cfg: StabilityConfig):
    """Online-softmax attention for one Q block of one head.

    q_blk: (bq, d) float64; k/v: (seq, d)/(seq, dv) float64.
    Returns (output, m, denom, kv_stats).
    """
    seq = k.shape[0]
    bq = q_blk.shape[0]
    dv = v.shape[1]
    m = np.full(bq, _NEG_INF, dtype=_F64)
    denom = np.zeros(bq, dtype=_F64)
    acc = np.zeros((bq, dv), dtype=_F64)
    q_pos = np.arange(q_start, q_start + bq)
    kv_stats: list[KVBlockStat] = []

    for kv_idx, ks in enumerate(range(0, seq, kv_block_len)):
        ke = min(ks + kv_block_len, seq)
        s = scores_block(q_blk, k[ks:ke], scale)
        if causal:
            k_pos = np.arange(ks, ke)
            future = k_pos[None, :] > q_pos[:, None]
            if future.any():
                s = np.where(future, _NEG_INF, s)

        m_blk = s.max(axis=1)
        finite_blk = np.isfinite(m_blk)
        with np.errstate(invalid="ignore"):
            p_blk = np.exp(s - m_blk[:, None])
        # Rows whose KV block is fully masked contribute exactly zero.
        p_blk = np.where(finite_blk[:, None], p_blk, 0.0)
        p_blk = _flush_subnormal(p_blk, cfg)
        l_blk = p_blk.sum(axis=1, dtype=_F64)
        kv_stats.append(KVBlockStat(kv_idx, ks, ke, m_blk.copy(), l_blk.copy()))

        m_new = np.maximum(m, m_blk)
        with np.errstate(invalid="ignore"):
            alpha = np.exp(m - m_new)
        alpha = np.where(np.isfinite(m), alpha, 0.0)

        with np.errstate(invalid="ignore"):
            p = np.exp(s - m_new[:, None])
        p = np.where(np.isfinite(s), p, 0.0)
        p = _flush_subnormal(p, cfg)

        denom = denom * alpha + p.sum(axis=1, dtype=_F64)
        acc = acc * alpha[:, None] + pv_block(p, v[ks:ke])
        m = m_new

    out = acc / denom[:, None]
    return out, m, denom, kv_stats


def run_chunked(q, k, v, q_block_len, kv_block_len, causal,
                cfg: StabilityConfig, on_q_block=None) -> ChunkedRun:
    """Run two-level blocked attention over all heads.

    q/k/v: (n_heads, seq_len, d) float64 arrays.
    on_q_block: optional callback(q_block_idx, q_start, q_end) invoked after
    each Q block becomes ready (used by the ready-query interface).
    """
    n_heads, seq, d = q.shape
    scale = 1.0 / np.sqrt(_F64(d))
    dv = v.shape[-1]
    output = np.empty((n_heads, seq, dv), dtype=_F64)
    all_q_blocks = []

    q_ranges = [(qs, min(qs + q_block_len, seq)) for qs in range(0, seq, q_block_len)]
    for head in range(n_heads):
        all_q_blocks.append([None] * len(q_ranges))

    for qi, (qs, qe) in enumerate(q_ranges):
        for head in range(n_heads):
            out, m, denom, kv_stats = attention_online(
                q[head, qs:qe], k[head], v[head], qs, causal,
                kv_block_len, scale, cfg)
            output[head, qs:qe] = out
            all_q_blocks[head][qi] = QBlockResult(
                q_block_idx=qi, q_start=qs, q_end=qe,
                output=out, m=m, denom=denom, kv_stats=kv_stats)
        if on_q_block is not None:
            on_q_block(qi, qs, qe)

    return ChunkedRun(
        n_heads=n_heads, seq_len=seq, head_dim=d,
        q_block_len=q_block_len, kv_block_len=kv_block_len, causal=causal,
        q_blocks=all_q_blocks, output=output)


def run_whole(q, k, v, kv_block_len, causal, cfg: StabilityConfig) -> ChunkedRun:
    """Whole-segment pass: a single Q block, identical KV merge sequence."""
    seq = q.shape[1]
    return run_chunked(q, k, v, seq, kv_block_len, causal, cfg)


def apply_rounding(x: np.ndarray, dtype: str, mode: str) -> np.ndarray:
    """Cast float64 output to the manifest dtype under the configured
    rounding mode (requirement 5)."""
    if dtype == "float64":
        return np.array(x, dtype=_F64, copy=True)
    if dtype != "float32":
        raise ValueError(f"unsupported dtype: {dtype}")
    y = x.astype(np.float32)
    if mode == ROUND_NEAREST_EVEN:
        return y
    if mode == ROUND_TOWARD_ZERO:
        # One-ulp correction toward zero whenever the round-to-nearest result
        # overshoots the exact float64 value in magnitude.
        over = np.abs(y.astype(_F64)) > np.abs(x)
        return np.where(over, np.nextafter(y, np.float32(0.0)), y)
    raise ValueError(f"unsupported rounding mode: {mode}")
