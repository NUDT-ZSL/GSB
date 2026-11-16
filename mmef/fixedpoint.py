"""Deterministic fixed-point arithmetic.

All fixed-point values are plain Python ints with ``bits`` fractional bits
(scale == 1 << bits). Every operation is exact integer arithmetic, so
results are identical on any platform and any run.
"""
from __future__ import annotations

import math


def round_half_away(value: float) -> int:
    """Deterministic round-half-away-from-zero (no banker's rounding)."""
    if value >= 0:
        return math.floor(value + 0.5)
    return -math.floor(-value + 0.5)


def quantize_scalar(value: float, bits: int, levels: int | None = None):
    """Two-stage deterministic quantization.

    Stage 1 (optional): snap ``value`` to the nearest of ``levels`` uniformly
    spaced quantization levels (量化档位).
    Stage 2: convert to a fixed-point integer with ``bits`` fractional bits
    (定点小数位宽).

    Returns ``(q_int, residual_level, residual_fp)``: the fixed-point integer
    plus the float rounding leftover of each stage. The fixed-point half unit
    is ``0.5 / (1 << bits)``; callers report residuals whose absolute value
    exceeds it as error sources.
    """
    scale = 1 << bits
    if levels and levels > 1:
        snapped = round_half_away(value * (levels - 1)) / (levels - 1)
    else:
        snapped = value
    residual_level = value - snapped
    q = round_half_away(snapped * scale)
    residual_fp = snapped - q / scale
    return q, residual_level, residual_fp


def fp_exp(x: int, bits: int, max_terms: int = 24):
    """Fixed-point exp for x <= 0 via truncated Taylor series.

    Returns ``(value, truncation_position)`` where truncation_position is the
    term index at which the series was cut: either the first term that
    underflowed to 0 in fixed-point, or ``max_terms``.
    """
    scale = 1 << bits
    term = scale  # x^0 / 0!
    total = scale
    trunc = max_terms
    for k in range(1, max_terms + 1):
        term = (term * x) >> bits
        term = term // k
        if term == 0:
            trunc = k
            break
        total += term
    if total < 0:
        total = 0
    return total, trunc


def fp_softmax(logits: list[int], bits: int, temp_fp: int):
    """Stable fixed-point softmax (max-subtracted form).

    ``logits`` and ``temp_fp`` are fixed-point ints with ``bits`` fractional
    bits. Returns ``(probs, truncations)``: fixed-point probabilities and the
    per-element exp truncation positions.
    """
    scaled = [(v << bits) // temp_fp for v in logits]
    m = max(scaled)
    exps = []
    truncs = []
    for v in scaled:
        e, t = fp_exp(v - m, bits)
        exps.append(e)
        truncs.append(t)
    total = sum(exps)
    probs = [(e << bits) // total for e in exps]
    return probs, truncs
