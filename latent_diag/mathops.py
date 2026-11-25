from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import EngineError


def _round_values(values: np.ndarray, mode: str) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if mode == "nearest_even":
        return np.rint(values)
    if mode == "nearest_away":
        return np.sign(values) * np.floor(np.abs(values) + 0.5)
    if mode == "floor":
        return np.floor(values)
    if mode == "ceil":
        return np.ceil(values)
    if mode == "trunc":
        return np.trunc(values)
    raise EngineError("E_ROUNDING_MODE_INVALID", f"unsupported rounding_mode: {mode}")


class Ops:
    """One operator sequence, with profile-specific storage and rounding."""

    profile = "base"
    itemsize = 8

    def from_float64(self, array: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def to_float64(self, array: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def const(self, array: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def mul(self, left: np.ndarray, right: np.ndarray | float) -> np.ndarray:
        raise NotImplementedError

    def add(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def sub(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def div(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def stats(self, array: np.ndarray) -> tuple[object, object, float, float]:
        raise NotImplementedError

    def scalar_to_float(self, value: object) -> float:
        return float(np.asarray(value, dtype=np.float64))


@dataclass
class Float64Ops(Ops):
    profile: str = "float64"
    itemsize: int = 8

    def from_float64(self, array: np.ndarray) -> np.ndarray:
        return np.ascontiguousarray(array, dtype=np.float64)

    def to_float64(self, array: np.ndarray) -> np.ndarray:
        return np.asarray(array, dtype=np.float64)

    def const(self, array: np.ndarray) -> np.ndarray:
        return np.asarray(array, dtype=np.float64)

    def mul(self, left: np.ndarray, right: np.ndarray | float) -> np.ndarray:
        return np.asarray(left * right, dtype=np.float64)

    def add(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        return np.asarray(left + right, dtype=np.float64)

    def sub(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        return np.asarray(left - right, dtype=np.float64)

    def div(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        return np.asarray(left / right, dtype=np.float64)

    def stats(self, array: np.ndarray) -> tuple[np.float64, np.float64, float, float]:
        mean = np.mean(array, dtype=np.float64)
        std = np.std(array, dtype=np.float64)
        return mean, std, float(mean), float(std)


@dataclass
class Float32Ops(Ops):
    profile: str = "float32"
    itemsize: int = 4

    def from_float64(self, array: np.ndarray) -> np.ndarray:
        return np.ascontiguousarray(array, dtype=np.float32)

    def to_float64(self, array: np.ndarray) -> np.ndarray:
        return np.asarray(array, dtype=np.float64)

    def const(self, array: np.ndarray) -> np.ndarray:
        return np.asarray(array, dtype=np.float32)

    def mul(self, left: np.ndarray, right: np.ndarray | float) -> np.ndarray:
        factor = (
            np.float32(right)
            if np.isscalar(right)
            else np.asarray(right, dtype=np.float32)
        )
        return np.asarray(left * factor, dtype=np.float32)

    def add(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        return np.asarray(left + right, dtype=np.float32)

    def sub(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        return np.asarray(left - right, dtype=np.float32)

    def div(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        return np.asarray(left / right, dtype=np.float32)

    def stats(self, array: np.ndarray) -> tuple[np.float32, np.float32, float, float]:
        mean = np.mean(array, dtype=np.float32)
        std = np.std(array, dtype=np.float32)
        return mean, std, float(mean), float(std)


@dataclass
class FixedOps(Ops):
    quant_scale: int
    rounding_mode: str
    profile: str = "fixed"
    itemsize: int = 8

    def __post_init__(self) -> None:
        if self.quant_scale <= 0:
            raise EngineError(
                "E_QUANT_SCALE_INVALID",
                "quant_scale must be positive",
                details={"quant_scale": self.quant_scale},
            )

    def _quantize(self, values: np.ndarray) -> np.ndarray:
        scaled = np.asarray(values, dtype=np.float64) * float(self.quant_scale)
        rounded = _round_values(scaled, self.rounding_mode)
        return np.ascontiguousarray(rounded, dtype=np.int64)

    def from_float64(self, array: np.ndarray) -> np.ndarray:
        return self._quantize(array)

    def to_float64(self, array: np.ndarray) -> np.ndarray:
        return np.asarray(array, dtype=np.float64) / float(self.quant_scale)

    def const(self, array: np.ndarray) -> np.ndarray:
        return self._quantize(array)

    def mul(self, left: np.ndarray, right: np.ndarray | float) -> np.ndarray:
        if np.isscalar(right):
            factor = np.float64(right)
            return self._quantize(self.to_float64(left) * factor)
        return self._quantize(self.to_float64(left) * self.to_float64(right))

    def add(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        return np.ascontiguousarray(
            np.asarray(left, dtype=np.int64) + np.asarray(right, dtype=np.int64),
            dtype=np.int64,
        )

    def sub(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        return np.ascontiguousarray(
            np.asarray(left, dtype=np.int64) - np.asarray(right, dtype=np.int64),
            dtype=np.int64,
        )

    def div(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        denominator = self.to_float64(right)
        if np.any(denominator == 0.0):
            raise EngineError(
                "E_NORMALIZATION_INVALID",
                "zero normalization denominator in fixed profile",
            )
        return self._quantize(self.to_float64(left) / denominator)

    def stats(self, array: np.ndarray) -> tuple[np.int64, np.int64, float, float]:
        values = self.to_float64(array)
        mean = float(np.mean(values, dtype=np.float64))
        std = float(np.std(values, dtype=np.float64))
        mean_q = np.int64(
            _round_values(
                np.asarray(mean * self.quant_scale), self.rounding_mode
            ).item()
        )
        std_q = np.int64(
            _round_values(
                np.asarray(std * self.quant_scale), self.rounding_mode
            ).item()
        )
        return mean_q, std_q, float(mean_q) / self.quant_scale, float(std_q) / self.quant_scale


def make_ops(profile: str, quant_scale: int, rounding_mode: str) -> Ops:
    if profile == "float64":
        return Float64Ops()
    if profile == "float32":
        return Float32Ops()
    if profile == "fixed":
        return FixedOps(quant_scale=quant_scale, rounding_mode=rounding_mode)
    raise EngineError(
        "E_DTYPE_PROFILE_INVALID",
        f"unsupported dtype_profile: {profile}",
        details={"dtype_profile": profile},
    )

