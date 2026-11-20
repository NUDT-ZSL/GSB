"""Float64 numerical primitives: rounding, subnormal protection, accumulation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import NumericsConfig, ROUNDING_MODES
from .errors import E_ROUNDING_MODE_UNKNOWN, business_error


def _as_float64(values: np.ndarray) -> np.ndarray:
    return np.asarray(values, dtype=np.float64)


def round_values(values: np.ndarray, mode: str) -> np.ndarray:
    """Round float64 values to integers using an explicit rule."""

    if mode not in ROUNDING_MODES:
        raise business_error(
            E_ROUNDING_MODE_UNKNOWN,
            "rounding",
            "unknown",
            f"unknown rounding mode: {mode!r}",
            known_modes=list(ROUNDING_MODES),
        )
    array = _as_float64(values)
    if mode == "nearest_even":
        return np.rint(array)
    if mode == "nearest_away":
        return np.sign(array) * np.floor(np.abs(array) + 0.5)
    if mode == "truncate":
        return np.trunc(array)
    if mode == "floor":
        return np.floor(array)
    return np.ceil(array)


def round_to_step(
    values: np.ndarray,
    step: float,
    mode: str,
) -> np.ndarray:
    """Round values to a fixed absolute float64 grid."""

    array = _as_float64(values)
    step_value = float(step)
    if step_value == 0.0:
        return array
    if not np.isfinite(step_value) or step_value < 0.0:
        raise ValueError("rounding step must be finite and non-negative")
    return round_values(array / step_value, mode) * step_value


def protect_subnormal(
    values: np.ndarray,
    policy: str,
    threshold: float,
) -> np.ndarray:
    array = _as_float64(values)
    if policy == "preserve":
        return array
    limit = float(threshold)
    magnitude = np.abs(array)
    mask = (magnitude > 0.0) & (magnitude < limit)
    if policy == "flush_to_zero":
        return np.where(mask, 0.0, array)
    if policy == "clamp_to_threshold":
        return np.where(mask, np.sign(array) * limit, array)
    raise AssertionError(f"validated subnormal policy not implemented: {policy}")


def apply_numeric_policy(
    values: np.ndarray,
    config: NumericsConfig,
    *,
    rounding_step: float = 0.0,
) -> np.ndarray:
    result = _as_float64(values)
    if rounding_step > 0.0:
        result = round_to_step(result, rounding_step, config.rounding_mode)
    return protect_subnormal(
        result,
        config.subnormal_policy,
        config.subnormal_threshold,
    )


@dataclass(frozen=True)
class AccumulationResult:
    values: np.ndarray
    additions: int
    rounding_applied: bool


def accumulate_weighted_terms(
    terms: np.ndarray,
    weights: np.ndarray,
    config: NumericsConfig,
) -> AccumulationResult:
    """Accumulate ``sum_k terms[t,k,d] * weights[t,k,d]`` in stable k order.

    ``terms`` and ``weights`` are token-major arrays shaped ``(T, K, D)``.
    When accumulation rounding is enabled, the partial sum is rounded after
    every add, which makes the error attributable to this stage.
    """

    term_array = _as_float64(terms)
    weight_array = _as_float64(weights)
    if term_array.shape != weight_array.shape:
        raise ValueError("terms and weights must have identical shapes")
    if term_array.ndim != 3:
        raise ValueError("terms must be shaped (tokens, hits, embed_dim)")
    partial = np.zeros(term_array.shape[::2], dtype=np.float64)
    for hit in range(term_array.shape[1]):
        term = term_array[:, hit, :] * weight_array[:, hit, :]
        term = protect_subnormal(
            term,
            config.subnormal_policy,
            config.subnormal_threshold,
        )
        partial = partial + term
        if config.accum_rounding_enabled and config.accum_rounding_step > 0.0:
            partial = round_to_step(
                partial,
                config.accum_rounding_step,
                config.rounding_mode,
            )
        partial = protect_subnormal(
            partial,
            config.subnormal_policy,
            config.subnormal_threshold,
        )
    return AccumulationResult(
        values=partial,
        additions=term_array.shape[1],
        rounding_applied=bool(
            config.accum_rounding_enabled and config.accum_rounding_step > 0.0
        ),
    )


def finite_or_none(value: float) -> float | None:
    result = float(value)
    return result if np.isfinite(result) else None


def observed_resolution(values: np.ndarray, fallback: float) -> dict[str, Any]:
    """Estimate the smallest nonzero spacing visible in an observed array."""

    flat = np.abs(_as_float64(values).reshape(-1))
    flat = flat[np.isfinite(flat)]
    if flat.size == 0:
        return {"value": float(fallback), "method": "configured_fallback"}
    unique = np.unique(flat)
    if unique.size < 2:
        return {"value": float(fallback), "method": "configured_fallback"}
    diffs = np.diff(unique)
    diffs = diffs[diffs > 0.0]
    if diffs.size == 0:
        return {"value": float(fallback), "method": "configured_fallback"}
    return {"value": float(diffs.min()), "method": "minimum_positive_spacing"}
