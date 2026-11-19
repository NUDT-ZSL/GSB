"""Switchable numeric precision execution layer.

Three tiers share exactly the same operator semantics:

* ``float64`` / ``float32``: NumPy dtypes; the same Python call path is used.
* ``fixed``: scaled integer arithmetic on Python arbitrary-precision integers
  held in object arrays, so intermediate products never silently overflow.
  Every real number ``x`` is stored as the integer ``round(x * scale)``;
  addition/subtraction keep the scale, multiplication/divide rescale
  explicitly with the configured rounding mode, and ``exp`` / ``sigmoid`` use
  the same reduction+series formula on integers.

The pipeline never branches on precision tier except inside this module, so a
precision switch cannot change feature construction semantics or tie-breaking.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from .config import FixedConfig
from .errors import BusinessError

FLOAT_DTYPES = {"float64": np.float64, "float32": np.float32}


def _round_divide(numerator: int, denominator: int, rounding: str) -> int:
    """Integer division with an explicit, deterministic rounding mode."""
    if denominator == 0:
        raise ZeroDivisionError("fixed-point division by zero")
    q, r = divmod(numerator, denominator)
    if r == 0:
        return q
    neg = (numerator < 0) ^ (denominator < 0)
    if rounding == "trunc":
        # truncation toward zero: divmod floors; step back for negatives
        return q + 1 if neg else q
    if rounding == "floor":
        # divmod already returns the floored quotient
        return q
    # nearest modes work with the absolute remainder against |denominator|
    twice_r = abs(2 * r)
    twice_d = abs(denominator)
    if twice_r < twice_d:
        return q
    if twice_r > twice_d:
        # nearest is one above the floor for both signs
        return q + 1
    # exact tie: q is the floored quotient, q+1 is the other neighbor
    if rounding == "half_up":
        return q + 1 if not neg else q
    # half_even: choose the neighbor whose absolute value is even
    if (q + 1) % 2 == 0:
        return q + 1
    return q


@dataclass(frozen=True)
class PrecisionContext:
    mode: str
    fixed: FixedConfig | None = None

    @property
    def is_fixed(self) -> bool:
        return self.mode == "fixed"

    @property
    def dtype(self) -> np.dtype:
        if self.mode not in FLOAT_DTYPES:
            raise BusinessError(
                "E_PRECISION_UNKNOWN", "precision",
                f"mode {self.mode!r} has no floating dtype",
                context={"bad_value": self.mode},
            )
        return np.dtype(FLOAT_DTYPES[self.mode])

    # ---- scalar / vector ingress -----------------------------------------
    def encode(self, value: float | Iterable[float]):
        """Bring an external float64 value into this precision representation."""
        if self.is_fixed:
            arr = np.asarray(value)
            if arr.ndim == 0:
                return self._encode_scalar(float(value))
            return np.array([self._encode_scalar(float(v))
                             for v in arr.reshape(-1)],
                            dtype=object).reshape(arr.shape)
        return np.asarray(value, dtype=self.dtype)

    def _encode_scalar(self, value: float) -> int:
        # tenfold numerator keeps one extra decimal before the explicit rounding
        numerator = int(round(value * self.fixed.scale * 10))
        encoded = _round_divide(numerator, 10, self.fixed.rounding)
        self._check_width(encoded, "encode")
        return encoded

    def _check_width(self, integer: int, operation: str) -> None:
        """Raise when a fixed-point integer exceeds the configured int range."""
        limit = 1 << (self.fixed.int_bits - self.fixed.fraction_bits)
        if abs(integer) > limit * self.fixed.scale:
            raise BusinessError(
                "E_VALUE_RANGE", "precision",
                f"fixed-point integer exceeds int_bits budget during {operation}",
                field="precision.fixed.int_bits",
                context={"value_magnitude": abs(integer),
                         "limit_magnitude": limit * self.fixed.scale,
                         "operation": operation},
            )

    def decode(self, value):
        """Return the float64 view of a representation (for reporting only)."""
        if self.is_fixed:
            if np.isscalar(value):
                return float(value) / self.fixed.scale
            arr = np.asarray(value)
            flat = np.array([float(v) for v in arr.reshape(-1)], dtype=np.float64)
            return (flat / self.fixed.scale).reshape(arr.shape)
        return np.asarray(value, dtype=np.float64)

    # ---- arithmetic -------------------------------------------------------
    def add(self, a, b):
        if self.is_fixed:
            return np.asarray(a, dtype=object) + np.asarray(b, dtype=object)
        return np.asarray(a, dtype=self.dtype) + np.asarray(b, dtype=self.dtype)

    def sub(self, a, b):
        if self.is_fixed:
            return np.asarray(a, dtype=object) - np.asarray(b, dtype=object)
        return np.asarray(a, dtype=self.dtype) - np.asarray(b, dtype=self.dtype)

    def mul(self, a, b):
        if self.is_fixed:
            scale = self.fixed.scale
            product = np.asarray(a, dtype=object) * np.asarray(b, dtype=object)
            return self._rescale_array(product, scale * scale, scale)
        return np.asarray(a, dtype=self.dtype) * np.asarray(b, dtype=self.dtype)

    def div(self, a, b):
        if self.is_fixed:
            numerator = np.asarray(a, dtype=object) * self.fixed.scale
            return self._rescale_array(numerator, np.asarray(b, dtype=object), 1)
        return np.asarray(a, dtype=self.dtype) / np.asarray(b, dtype=self.dtype)

    def _rescale_array(self, numerator, denominator, extra: int) -> np.ndarray:
        rounding = self.fixed.rounding
        num = np.asarray(numerator)
        den = np.asarray(denominator)
        if num.ndim == 0:
            return _round_divide(int(num) * extra, int(den), rounding)
        den_bcast = np.broadcast_to(den, num.shape)
        out = np.empty(num.shape, dtype=object)
        for idx in np.ndindex(num.shape):
            out[idx] = _round_divide(int(num[idx]) * extra,
                                     int(den_bcast[idx]), rounding)
        return out

    def scale_float(self, value: float) -> float:
        """Round an intermediate float constant through this tier's precision."""
        if self.is_fixed:
            return self.decode(self._encode_scalar(value))
        return float(np.asarray(value, dtype=self.dtype))

    # ---- nonlinear primitives --------------------------------------------
    def exp(self, x):
        """Exponential with identical reduction+series semantics on every tier."""
        if self.is_fixed:
            return self._fixed_exp(np.asarray(x, dtype=object))
        x_arr = np.asarray(x, dtype=self.dtype)
        # exp(x) = 2^k * exp(r), r in [-ln2/2, ln2/2], k = round(x/ln2)
        ln2 = np.asarray(math.log(2.0), dtype=self.dtype)
        k = np.rint(x_arr / ln2)
        r = x_arr - k * ln2
        result = self._float_exp_series(r)
        return np.ldexp(result, k.astype(np.int64))

    def _float_exp_series(self, r) -> np.ndarray:
        terms = self.fixed.exp_terms if self.is_fixed else 24
        total = np.zeros_like(r)
        term = np.ones_like(r)
        for n in range(1, terms + 1):
            term = term * r / np.asarray(n, dtype=self.dtype)
            total = total + term
        return total + np.asarray(1.0, dtype=self.dtype)

    def _fixed_exp(self, x: np.ndarray) -> np.ndarray:
        cfg = self.fixed
        ln2_scaled = self._encode_scalar(math.log(2.0))
        out = np.empty(x.shape, dtype=object)
        for idx in np.ndindex(x.shape):
            xi = int(x[idx])
            # k = round(x / ln2) implemented entirely in fixed point
            k = _round_divide(xi, ln2_scaled, cfg.rounding)
            # both xi and k*ln2_scaled are already S-scaled; subtract directly
            r = xi - k * ln2_scaled
            # series: exp(r) = 1 + r + r^2/2! + ..., all in scaled integers
            total = cfg.scale
            term = cfg.scale
            for n in range(1, cfg.exp_terms + 1):
                term = _round_divide(term * r, n * cfg.scale, cfg.rounding)
                total += term
            if k >= 0:
                out[idx] = total << k
            else:
                out[idx] = _round_divide(total, 1 << (-k), cfg.rounding)
        return out

    def sigmoid(self, x):
        """Numerically stable sigmoid: branch on sign, shared formula per tier."""
        if self.is_fixed:
            x_arr = np.asarray(x, dtype=object)
            out = np.empty(x_arr.shape, dtype=object)
            scale = self.fixed.scale
            for idx in np.ndindex(x_arr.shape):
                xi = int(x_arr[idx])
                if xi >= 0:
                    ex = int(self._fixed_exp(np.array(xi, dtype=object))[()])
                    # sigmoid = ex / (ex + 1); scaled numerator ex*S over (ex + S)
                    out[idx] = _round_divide(ex * scale, ex + scale,
                                             self.fixed.rounding)
                else:
                    ex = int(self._fixed_exp(np.array(-xi, dtype=object))[()])
                    # sigmoid = 1 / (1 + exp(-x)); scaled: S^2 / (ex + S)
                    out[idx] = _round_divide(scale * scale, ex + scale,
                                             self.fixed.rounding)
            return out
        x_arr = np.asarray(x, dtype=self.dtype)
        out = np.empty_like(x_arr)
        pos = x_arr >= 0
        exp_pos = self.exp(x_arr[pos])
        out[pos] = exp_pos / (exp_pos + np.asarray(1.0, dtype=self.dtype))
        exp_neg = self.exp(-x_arr[~pos])
        out[~pos] = np.asarray(1.0, dtype=self.dtype) / (
            np.asarray(1.0, dtype=self.dtype) + exp_neg)
        return out

    def maximum(self, a, b):
        if self.is_fixed:
            return np.maximum(np.asarray(a, dtype=object),
                              np.asarray(b, dtype=object))
        return np.maximum(np.asarray(a, dtype=self.dtype),
                          np.asarray(b, dtype=self.dtype))

    def abs(self, a):
        if self.is_fixed:
            return np.abs(np.asarray(a, dtype=object))
        return np.abs(np.asarray(a, dtype=self.dtype))

    def flush_subnormal(self, a):
        """Flush subnormal floating values to zero; no-op for the fixed tier."""
        if self.is_fixed:
            return a
        arr = np.asarray(a)
        tiny = np.finfo(self.dtype).tiny
        return np.where((arr != 0) & (np.abs(arr) < tiny), 0,
                        arr).astype(self.dtype)

    def linear_dot(self, feature_matrix: np.ndarray, weights: Sequence[float],
                   bias: float):
        """Linear score using this tier's operators for every multiply/add."""
        acc = self.encode(np.zeros(feature_matrix.shape[0], dtype=np.float64))
        for j, weight in enumerate(weights):
            w = self.encode(float(weight))
            acc = self.add(acc, self.mul(feature_matrix[:, j], w))
        acc = self.add(acc, self.encode(float(bias)))
        return acc


def make_context(mode: str, fixed: FixedConfig | None) -> PrecisionContext:
    if mode in FLOAT_DTYPES:
        return PrecisionContext(mode=mode)
    if mode == "fixed":
        if fixed is None:
            raise BusinessError("E_SCALE_INVALID", "precision",
                                "fixed tier selected but no fixed-point config present")
        return PrecisionContext(mode="fixed", fixed=fixed)
    raise BusinessError(
        "E_PRECISION_UNKNOWN", "precision",
        f"unknown precision tier {mode!r}",
        context={"bad_value": mode,
                 "known": ["float64", "float32", "fixed"]},
    )
