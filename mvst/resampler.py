"""Windowed-sinc resampling, float-domain gain and saturated quantization.

The resampler is fully deterministic: given identical integer input samples,
sample rate, gain and window parameters it produces bit-identical output, so
two runs on the same input yield equal reports apart from ``generated_at``.

Each invocation allocates its own kernel/output buffers (nothing mutable is
shared between worker threads).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from fractions import Fraction

from . import MASTER_SAMPLE_RATE
from .errors import MvstError
from .timebase import round_half_away

DEFAULT_HALF_WINDOW = 32
DEFAULT_CUTOFF_RATIO = 0.90
_SAT_MIN = -32768
_SAT_MAX = 32767


@dataclass
class ResampleResult:
    samples: list[int]
    source_samples: int
    resampled_samples: int
    clip_count: int
    peak: int
    rms: float
    exact_master_samples: Fraction
    length_delta: int
    reproducible: bool


def _sinc(x: float) -> float:
    if x == 0.0:
        return 1.0
    px = math.pi * x
    return math.sin(px) / px


def _round_half_away_float(value: float) -> int:
    """Half-away-from-zero rounding in the float domain (post-gain stage)."""
    if value >= 0.0:
        return int(math.floor(value + 0.5))
    return -int(math.floor(-value + 0.5))


def resample_track(
    track_index: int,
    samples: list[int],
    source_rate: int,
    gain_db: float,
    half_window: int = DEFAULT_HALF_WINDOW,
    cutoff_ratio: float = DEFAULT_CUTOFF_RATIO,
) -> ResampleResult:
    """Resample mono PCM samples to 48 kHz, apply gain and saturate to 16 bit."""
    # Deterministic test hook: a worker asked to fail must surface E_RESAMPLE
    # without disturbing the other workers' independent buffers.
    fail_set = {int(x) for x in os.environ.get("MVST_FAIL_TRACK", "").split(",") if x}
    if track_index in fail_set:
        raise MvstError("E_RESAMPLE", track=track_index)

    n_in = len(samples)
    ratio = Fraction(source_rate, MASTER_SAMPLE_RATE)          # input period on output grid
    exact_out = Fraction(n_in * MASTER_SAMPLE_RATE, source_rate)
    n_out = round_half_away(exact_out)

    # Anti-imaging cutoff: never above the smaller of the two Nyquist limits.
    cutoff = cutoff_ratio * min(1.0, float(ratio))
    gain = 10.0 ** (gain_db / 20.0)

    # Per-call (therefore per-thread) writable buffers.  Normalize against
    # the positive 16-bit full-scale value so a +32767 input maps to 1.0.
    full_scale = 32767
    normalized = [s / full_scale for s in samples]
    out: list[int] = [0] * n_out
    clip_count = 0
    peak = 0
    square_sum = 0

    for n in range(n_out):
        center = n * float(ratio)
        k0 = max(0, math.floor(center) - half_window)
        k1 = min(n_in - 1, math.floor(center) + half_window)
        acc = 0.0
        weight_sum = 0.0
        for k in range(k0, k1 + 1):
            d = (k - center) * cutoff
            # Hann window over the finite support, scaled by the cutoff.
            w = 0.5 * (1.0 + math.cos(math.pi * (k - center) / (half_window + 1)))
            weight = cutoff * _sinc(d) * w
            acc += normalized[k] * weight
            weight_sum += weight
        if weight_sum != 0.0:
            acc /= weight_sum
        scaled = acc * gain * full_scale
        q = _round_half_away_float(scaled)
        if q > _SAT_MAX:
            q = _SAT_MAX
            clip_count += 1
        elif q < _SAT_MIN:
            q = _SAT_MIN
            clip_count += 1
        out[n] = q
        # Peak is reported against the positive full-scale value (32767);
        # the legal negative full-scale -32768 is not a positive peak.
        if q > peak:
            peak = q
        square_sum += q * q

    rms = round(math.sqrt(square_sum / n_out), 6) if n_out else 0.0
    # Difference between the emitted integer length and the exact rational
    # length.  For integer-rate conversions this is an integer (often zero).
    length_delta_fraction = n_out - exact_out
    length_delta = (int(length_delta_fraction)
                    if length_delta_fraction.denominator == 1 else 0)
    return ResampleResult(
        samples=out,
        source_samples=n_in,
        resampled_samples=n_out,
        clip_count=clip_count,
        peak=peak,
        rms=rms,
        exact_master_samples=exact_out,
        length_delta=length_delta,
        reproducible=True,
    )
