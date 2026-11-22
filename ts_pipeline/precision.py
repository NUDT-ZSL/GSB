"""Shared precision-profile execution semantics and fixed-point audit helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from .errors import ConfigInvalid
from .utils import guard_subnormal


def fixed_round(values: np.ndarray, rounding: str) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if rounding == "nearest_even":
        return np.rint(values)
    if rounding == "nearest_away":
        return np.sign(values) * np.floor(np.abs(values) + 0.5)
    if rounding == "toward_zero":
        return np.trunc(values)
    if rounding == "floor":
        return np.floor(values)
    if rounding == "ceil":
        return np.ceil(values)
    raise ConfigInvalid(
        "precision.rounding",
        f"unsupported fixed-point rounding mode: {rounding}",
    )


def quantize_fixed_scalar(
    value: float,
    scale: int,
    rounding: str,
) -> tuple[int, float]:
    """Return (integer code, dequantized float) for one scalar."""

    if not math.isfinite(float(value)):
        raise ConfigInvalid("precision", "cannot quantize a non-finite value")
    scaled = float(value) * int(scale)
    rounded = float(fixed_round(np.asarray([scaled], dtype=np.float64), rounding)[0])
    integer = int(rounded)
    if integer > np.iinfo(np.int64).max or integer < np.iinfo(np.int64).min:
        raise ConfigInvalid(
            "precision.fixed_scale",
            "fixed-point value exceeds int64 range for the configured scale",
        )
    return integer, float(integer) / int(scale)


@dataclass(frozen=True)
class Quantizer:
    profile: str
    fixed_scale: int = 1_000_000
    rounding: str = "nearest_even"

    @classmethod
    def identity(cls) -> "Quantizer":
        return cls(profile="float64", fixed_scale=1_000_000, rounding="nearest_even")

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "Quantizer":
        precision = config["precision"]
        return cls(
            profile=str(precision["profile"]),
            fixed_scale=int(precision["fixed_scale"]),
            rounding=str(precision["rounding"]),
        )

    @property
    def is_fixed(self) -> bool:
        return self.profile == "fixed_point"

    def describe(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "fixed_scale": self.fixed_scale,
            "rounding": self.rounding,
            "semantics": (
                "core arithmetic is float64; profile conversion is applied at "
                "feature, target and model-parameter boundaries"
            ),
        }

    def quantize_scalar(self, value: float) -> float:
        value = float(value)
        if self.profile == "float64":
            return value
        if self.profile == "float32":
            return float(np.float32(value))
        if self.profile == "fixed_point":
            _, dequantized = quantize_fixed_scalar(
                value,
                self.fixed_scale,
                self.rounding,
            )
            return dequantized
        raise ConfigInvalid(
            "precision.profile",
            f"unsupported precision profile: {self.profile}",
        )

    def quantize_array(self, values: np.ndarray) -> np.ndarray:
        array = np.ascontiguousarray(values, dtype=np.float64)
        if self.profile == "float64":
            return array.copy()
        if self.profile == "float32":
            return np.ascontiguousarray(array.astype(np.float32).astype(np.float64))
        if self.profile == "fixed_point":
            scaled = array * float(self.fixed_scale)
            rounded = fixed_round(scaled, self.rounding)
            if np.max(np.abs(rounded), initial=0.0) > np.iinfo(np.int64).max:
                raise ConfigInvalid(
                    "precision.fixed_scale",
                    "fixed-point array exceeds int64 range",
                )
            return np.ascontiguousarray(rounded / float(self.fixed_scale))
        raise ConfigInvalid(
            "precision.profile",
            f"unsupported precision profile: {self.profile}",
        )

    def quantize_integer_codes(self, values: np.ndarray) -> np.ndarray:
        if not self.is_fixed:
            raise ConfigInvalid(
                "precision.profile",
                "integer codes are only defined for the fixed_point profile",
            )
        array = np.asarray(values, dtype=np.float64)
        return fixed_round(array * float(self.fixed_scale), self.rounding).astype(
            np.int64
        )


def quantization_audit(
    probes: list[float],
    quantizer: Quantizer,
) -> list[dict[str, Any]]:
    """Expose independently replayable fixed-point conversions."""

    records: list[dict[str, Any]] = []
    for value in probes:
        if quantizer.is_fixed:
            code, dequantized = quantize_fixed_scalar(
                float(value),
                quantizer.fixed_scale,
                quantizer.rounding,
            )
            records.append(
                {
                    "original": float(value),
                    "scale": quantizer.fixed_scale,
                    "rounding": quantizer.rounding,
                    "integer_code": code,
                    "dequantized": dequantized,
                    "replayed": float(code) / float(quantizer.fixed_scale),
                }
            )
        elif quantizer.profile == "float32":
            casted = float(np.float32(value))
            records.append(
                {
                    "original": float(value),
                    "scale": None,
                    "rounding": "ieee754_float32_cast",
                    "integer_code": None,
                    "dequantized": casted,
                    "replayed": casted,
                }
            )
        else:
            records.append(
                {
                    "original": float(value),
                    "scale": None,
                    "rounding": "identity",
                    "integer_code": None,
                    "dequantized": float(value),
                    "replayed": float(value),
                }
            )
    return records


def guarded_cast(value: float, quantizer: Quantizer, subnormal_guard: bool) -> float:
    return guard_subnormal(quantizer.quantize_scalar(value), subnormal_guard)
