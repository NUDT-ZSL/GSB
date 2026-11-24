"""Shared in-flight sampling budget accounting."""

from __future__ import annotations


# A primary sample carries a ray state, per-step trace accumulator and Python
# container overhead.  One KiB is deliberately conservative for scheduling;
# changing this constant never changes sampling density or numerical precision.
SAMPLE_BUFFER_BYTES = 1024


def tile_buffer_bytes(width: int, height: int, samples_per_pixel: int) -> int:
    return width * height * samples_per_pixel * SAMPLE_BUFFER_BYTES

