"""Rational-ratio polyphase resampling.

The ratio ``fs_out / fs_in`` is reduced to coprime integers ``P / Q``
(upsample / downsample).  A windowed-sinc lowpass is built on the dense
``fs_in * P`` high-rate grid and split into ``Q`` polyphase branches.

For output frame ``j`` the input-side mapping is::

    n = floor(j * Q / P)        # nearest input frame
    r = j * Q - n * P           # polyphase branch, 0 <= r < P

Branch ``r`` tap ``t`` uses the prototype coefficient at high-rate index
``r + (t - center) * P``.  Each branch is normalized to unit DC gain.
Input and output share the t=0 instant, so the first input and first output
sample both land on frame 0 and multi-track alignment stays sample-accurate.
"""

from fractions import Fraction

import numpy as np

# Taps per polyphase branch; 16 gives a flat passband while staying cheap.
TAPS_PER_PHASE = 16


def rational_ratio(fs_in, fs_out):
    """Return coprime ``(P, Q)`` such that ``fs_out / fs_in == P / Q``."""
    fraction = Fraction(fs_out, fs_in)
    return fraction.numerator, fraction.denominator


def expected_output_frames(n_in, fs_in, fs_out):
    """Rounded output length: ``round(n_in * fs_out / fs_in)``."""
    return int(np.floor(n_in * fs_out / fs_in + 0.5))


def _build_phases(p, q):
    """Construct ``p`` branches from a dense high-rate prototype filter.

    Branch ``r`` holds prototype taps at high-rate indices
    ``r + (t - center) * p`` so every residue class mod ``p`` is covered.
    """
    k = max(p, q)
    span = 2 * TAPS_PER_PHASE + 1
    center = TAPS_PER_PHASE
    tap_offsets = (np.arange(span) - center) * p
    phases = np.zeros((p, span), dtype=np.float64)
    half = TAPS_PER_PHASE * p
    for residue in range(p):
        m = (tap_offsets + residue).astype(np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            sinc = np.where(m == 0.0, 1.0, np.sin(np.pi * m / k) / (np.pi * m))
        window = 0.5 * (1.0 + np.cos(np.pi * m / half))
        phases[residue] = sinc * window
    phases /= np.sum(phases, axis=1, keepdims=True)
    return phases


def _resample_poly(samples, p, q):
    if p == 1 and q == 1:
        return samples.astype(np.float64, copy=True)

    phases = _build_phases(p, q)
    span = phases.shape[1]
    n_out = expected_output_frames(samples.shape[0], q, p)
    out = np.zeros(n_out, dtype=np.float64)
    base = (np.arange(span) - (span - 1) // 2).astype(np.int64)

    block = 4096
    for start in range(0, n_out, block):
        end = min(start + block, n_out)
        j = np.arange(start, end, dtype=np.int64)
        # Input frame n and polyphase branch r for output j.
        scaled = q * j
        n = scaled // p
        residue = scaled - n * p
        idx = n[:, None] + base[None, :]
        valid = (idx >= 0) & (idx < samples.shape[0])
        safe_idx = np.clip(idx, 0, samples.shape[0] - 1)
        values = np.where(valid, samples[safe_idx], 0.0)
        taps = phases[residue]
        out[start:end] = np.sum(values * taps, axis=1)
    return out


def resample(samples, fs_in, fs_out):
    """Resample mono float ``samples`` from ``fs_in`` to ``fs_out``."""
    p, q = rational_ratio(fs_in, fs_out)
    return _resample_poly(samples, p, q)
