"""Precision, rounding and subnormal-number protection primitives.

These helpers are the single place where reduced precision arithmetic is
simulated, so every stage quantises values in exactly the same way and the
error propagation analysis can attribute deviations to a concrete step.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np

from .config import TrainConfig


def smallest_subnormal(dtype: np.dtype) -> float:
    """Return the smallest positive subnormal representable by ``dtype``."""
    return float(np.finfo(dtype).smallest_subnormal)


def round_array(values: np.ndarray, mode: str) -> np.ndarray:
    """Round float32-representable values with an explicit rounding mode."""
    values = np.asarray(values, dtype=np.float64)
    if mode == "half_even":
        return np.rint(values)
    if mode == "half_up":
        return np.floor(values + 0.5)
    if mode == "half_down":
        return np.ceil(values - 0.5)
    if mode == "trunc":
        return np.trunc(values)
    raise ValueError(f"unsupported rounding mode {mode!r}")


def quantize(values: np.ndarray, config: TrainConfig) -> np.ndarray:
    """Quantise an array to the configured precision with protection flags.

    For ``float64`` the values are returned unchanged.  For ``float32`` the
    requested rounding mode is emulated *before* the cast, and subnormal
    results are optionally flushed to signed zero.
    """
    values = np.asarray(values, dtype=np.float64)
    if config.precision == "float64":
        return values.astype(np.float64, copy=True)

    info32 = np.finfo(np.float32)
    # The native float32 cast is the base round-to-nearest-even behaviour.
    result = values.astype(np.float32).astype(np.float64)
    if config.rounding != "half_even":
        # Re-quantise through the mantissa scale for values large enough to
        # have a representable mantissa; subnormals keep the native cast.
        magnitude = np.ldexp(1.0, info32.nmant)
        representable = np.abs(values) >= np.finfo(np.float32).smallest_normal
        scaled = values[representable] * magnitude
        rounded = round_array(scaled, config.rounding) / magnitude
        result[representable] = rounded.astype(np.float32).astype(np.float64)

    if config.subnormal_protection:
        tiny = smallest_subnormal(np.float32)
        flush = (np.abs(result) <= tiny) & (result != 0.0)
        result = np.where(flush, np.copysign(0.0, result), result)
    return result


def quantize_scalar(value: float, config: TrainConfig) -> float:
    return float(quantize(np.asarray([value], dtype=np.float64), config)[0])


def had_subnormal(values: Iterable[float], precision: str) -> bool:
    """Report whether ``values`` contain a subnormal for the given precision."""
    if precision == "float64":
        tiny = smallest_subnormal(np.float64)
    else:
        tiny = smallest_subnormal(np.float32)
    return any(
        np.isfinite(v) and v != 0.0 and abs(v) <= tiny for v in values
    )
