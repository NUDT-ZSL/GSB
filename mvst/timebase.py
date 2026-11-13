"""Exact, floating-point-free time base conversions.

All three source time bases (video frame numbers, subtitle milliseconds and
audio sample positions) are converted with :class:`fractions.Fraction`.  The
same helper functions feed the audio alignment, subtitle snapping and container
index code paths so every intermediate value has a single numeric source.
"""

from __future__ import annotations

from fractions import Fraction

from . import MASTER_SAMPLE_RATE, VIDEO_FPS


def round_half_away(value: Fraction) -> int:
    """Round to the nearest integer, halves away from zero.

    ``round(Fraction)`` in Python uses banker's rounding; an explicit
    half-away rule keeps the behaviour unambiguous for every input.
    """
    if value >= 0:
        return int((value * 2 + 1) // 2)
    return -int(((-value) * 2 + 1) // 2)


def ms_to_master_sample(offset_ms: int) -> int:
    """Audio start sample: ``round(offset_ms * 48000 / 1000)``."""
    return round_half_away(Fraction(offset_ms * MASTER_SAMPLE_RATE, 1000))


def ms_to_frame(ms: int) -> int:
    """Subtitle millisecond -> nearest video frame: ``round(ms * 25 / 1000)``."""
    return round_half_away(Fraction(ms * VIDEO_FPS, 1000))


def source_samples_to_master_samples(source_samples: int, source_rate: int) -> Fraction:
    """Exact source length expressed on the master sample axis."""
    return Fraction(source_samples * MASTER_SAMPLE_RATE, source_rate)


def source_duration_sec(source_samples: int, source_rate: int) -> Fraction:
    return Fraction(source_samples, source_rate)


def frame_to_master_sample(frame: int) -> int:
    """Frame boundary position on the master sample axis (25 fps, 48 kHz)."""
    return frame * (MASTER_SAMPLE_RATE // VIDEO_FPS)
