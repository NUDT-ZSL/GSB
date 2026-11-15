"""Gain application and final soft-limited mix."""

import numpy as np


def apply_gain(samples, gain):
    """Linear gain; measured before this function is the pre-gain signal."""
    return samples * gain


def soft_limit(mix):
    """Saturating soft limiter applied only to the final summed mix.

    Samples inside the unit passband are untouched; outside the passband a
    tanh saturator smoothly converges to +/- 1 without hard clipping.
    """
    limited = np.empty_like(mix)
    inside = np.abs(mix) <= 1.0
    limited[inside] = mix[inside]
    outside = ~inside
    limited[outside] = np.tanh(mix[outside])
    return limited


def peak_level(samples):
    """Peak absolute amplitude in linear float units (0.0 for empty arrays)."""
    if samples.size == 0:
        return 0.0
    return float(np.max(np.abs(samples)))
