"""Float64 helpers with deterministic subnormal protection."""

from __future__ import annotations

import numpy as np


FLOAT_EPS = np.finfo(np.float64).eps
SUBNORMAL_CUTOFF = float(np.finfo(np.float64).tiny)
RELATIVE_DENOMINATOR_FLOOR = SUBNORMAL_CUTOFF


def flush_subnormal(values: np.ndarray | float) -> np.ndarray | float:
    """Flush subnormal float64 values to signed zero and reject non-finite values."""
    if np.isscalar(values):
        value = float(values)
        if not np.isfinite(value):
            return 0.0
        if abs(value) < SUBNORMAL_CUTOFF:
            return 0.0 if value >= 0.0 else -0.0
        return value

    array = np.asarray(values, dtype=np.float64)
    if not np.all(np.isfinite(array)):
        array = np.nan_to_num(array, nan=0.0, posinf=1.0, neginf=-1.0)
    return np.where(np.abs(array) < SUBNORMAL_CUTOFF, 0.0, array).astype(np.float64)


def strict_threshold(score: np.ndarray | float, threshold: float) -> np.ndarray | bool:
    score = flush_subnormal(score)
    safe_threshold = flush_subnormal(threshold)
    if np.isscalar(score):
        return bool(score >= safe_threshold)
    return np.asarray(score) >= safe_threshold


def tolerant_threshold(
    score: np.ndarray | float,
    threshold: float,
    tolerance: float = 1e-12,
    *,
    band_decision: str = "accept",
) -> tuple[np.ndarray | bool, np.ndarray | bool]:
    """Threshold with an explicit tolerance band.

    Values above the band are accepted, values below it are rejected. Values
    inside ``abs(score-threshold) <= tolerance`` are threshold-equivalent and
    use ``band_decision``: accept (default), reject, or strict. The second
    return value marks the tolerance band.
    """
    score = flush_subnormal(score)
    safe_threshold = flush_subnormal(np.asarray(threshold, dtype=np.float64))
    safe_tolerance = max(0.0, float(flush_subnormal(tolerance)))
    scalar = np.isscalar(score)
    array = np.atleast_1d(np.asarray(score, dtype=np.float64))
    threshold_array = np.broadcast_to(safe_threshold, array.shape)
    band = np.abs(array - threshold_array) <= safe_tolerance
    if band_decision == "strict":
        in_band_decision = array >= threshold_array
    elif band_decision == "accept":
        in_band_decision = np.ones_like(array, dtype=bool)
    elif band_decision == "reject":
        in_band_decision = np.zeros_like(array, dtype=bool)
    else:
        raise ValueError("band_decision must be accept, reject, or strict")
    accepted = np.where(band, in_band_decision, array >= threshold_array)
    accepted = np.where(array > threshold_array + safe_tolerance, True, accepted)
    accepted = np.where(array < threshold_array - safe_tolerance, False, accepted)
    if scalar:
        return bool(accepted[0]), bool(band[0])
    return accepted, band


def safe_ratio(numerator: float, denominator: float, *, default: float = 0.0) -> float:
    numerator = float(flush_subnormal(numerator))
    denominator = float(flush_subnormal(abs(denominator)))
    if denominator <= RELATIVE_DENOMINATOR_FLOOR:
        return default
    return numerator / denominator
