"""Shared numeric operator semantics for float64, float32, and fixed-point runs."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .errors import AppError
from .model import PrecisionConfig
from .util import digest_payload, stable_round

SUPPORTED_PROFILES = ("float64", "float32", "fixed")
SUPPORTED_ROUNDING = ("nearest_even", "half_up", "truncate", "floor")


def validate_precision(config: PrecisionConfig) -> None:
    if config.profile not in SUPPORTED_PROFILES:
        raise AppError(
            "E_PROFILE_INVALID",
            f"Unknown precision profile: {config.profile!r}",
            stage="precision.validate",
            field="profile",
            details={"allowed": list(SUPPORTED_PROFILES)},
        )
    if config.profile == "fixed":
        if not isinstance(config.fixed_scale, int) or config.fixed_scale <= 0:
            raise AppError(
                "E_PROFILE_INVALID",
                "Fixed-point scale must be a positive integer.",
                stage="precision.validate",
                field="fixed_scale",
                details={"fixed_scale": config.fixed_scale},
            )
        if config.rounding_mode not in SUPPORTED_ROUNDING:
            raise AppError(
                "E_PROFILE_INVALID",
                f"Unknown fixed-point rounding mode: {config.rounding_mode!r}",
                stage="precision.validate",
                field="rounding_mode",
                details={"allowed": list(SUPPORTED_ROUNDING)},
            )
    if not (math.isfinite(config.decision_quantum) and config.decision_quantum > 0):
        raise AppError(
            "E_PROFILE_INVALID",
            "Decision quantum must be finite and positive.",
            stage="precision.validate",
            field="decision_quantum",
        )
    if not (math.isfinite(config.subnormal_floor) and config.subnormal_floor > 0):
        raise AppError(
            "E_PROFILE_INVALID",
            "Subnormal floor must be finite and positive.",
            stage="precision.validate",
            field="subnormal_floor",
        )
    if config.comparison_strategy not in ("strict", "tolerance_band"):
        raise AppError(
            "E_PROFILE_INVALID",
            "Comparison strategy must be strict or tolerance_band.",
            stage="precision.validate",
            field="comparison_strategy",
        )
    if config.top_k <= 0:
        raise AppError(
            "E_PROFILE_INVALID",
            "top_k must be positive.",
            stage="precision.validate",
            field="top_k",
        )
    if config.tolerance < 0:
        raise AppError(
            "E_PROFILE_INVALID",
            "Tolerance cannot be negative.",
            stage="precision.validate",
            field="tolerance",
        )


def quantize_scalar(value: float, scale: int, rounding_mode: str) -> float:
    if not math.isfinite(value):
        return 0.0
    scaled = float(value) * int(scale)
    if rounding_mode == "nearest_even":
        integer = np.rint(scaled)
    elif rounding_mode == "half_up":
        integer = np.sign(scaled) * np.floor(np.abs(scaled) + 0.5)
    elif rounding_mode == "truncate":
        integer = np.trunc(scaled)
    elif rounding_mode == "floor":
        integer = np.floor(scaled)
    else:
        raise AppError(
            "E_PROFILE_INVALID",
            f"Unknown fixed-point rounding mode: {rounding_mode!r}",
            stage="precision.quantize",
            field="rounding_mode",
        )
    integer = np.clip(integer, -(2**62), 2**62)
    return float(integer / float(scale))


def quantize_array(
    values: np.ndarray | list[float] | tuple[float, ...],
    scale: int,
    rounding_mode: str,
) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64)
    flat = np.asarray([quantize_scalar(v, scale, rounding_mode) for v in arr.ravel()])
    return flat.reshape(arr.shape)


@dataclass(frozen=True)
class NumericOps:
    profile: str
    fixed_scale: int = 1_000_000
    rounding_mode: str = "nearest_even"
    subnormal_floor: float = 1e-12

    @classmethod
    def from_config(cls, config: PrecisionConfig) -> "NumericOps":
        validate_precision(config)
        return cls(
            profile=config.profile,
            fixed_scale=config.fixed_scale,
            rounding_mode=config.rounding_mode,
            subnormal_floor=config.subnormal_floor,
        )

    def cast(self, values: np.ndarray | list[float] | tuple[float, ...] | float) -> np.ndarray:
        arr = np.asarray(values, dtype=np.float64)
        if self.profile == "float64":
            return arr.astype(np.float64, copy=True)
        if self.profile == "float32":
            return arr.astype(np.float32).astype(np.float64)
        return quantize_array(arr, self.fixed_scale, self.rounding_mode)

    def cast_scalar(self, value: float) -> float:
        if self.profile == "float64":
            return float(value)
        if self.profile == "float32":
            return float(np.float32(value))
        return quantize_scalar(float(value), self.fixed_scale, self.rounding_mode)

    def protect_subnormal(self, values: np.ndarray) -> np.ndarray:
        arr = np.asarray(values, dtype=np.float64)
        arr = np.where(np.abs(arr) < self.subnormal_floor, 0.0, arr)
        return arr

    def normalize(self, values: np.ndarray | list[float] | tuple[float, ...]) -> np.ndarray:
        arr = self.cast(values)
        arr = self.protect_subnormal(arr)
        norm = math.sqrt(float(np.dot(arr, arr)))
        if not math.isfinite(norm) or norm < self.subnormal_floor:
            return np.zeros_like(arr, dtype=np.float64)
        return self.cast(arr / norm)

    def dot(self, left: np.ndarray, right: np.ndarray) -> float:
        value = float(np.dot(self.cast(left), self.cast(right)))
        return self.cast_scalar(value)

    def add(self, left: float, right: float) -> float:
        return self.cast_scalar(float(left) + float(right))

    def multiply(self, left: float, right: float) -> float:
        return self.cast_scalar(float(left) * float(right))


def stable_quantize(value: float, quantum: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return stable_round(round(float(value) / quantum) * quantum, 12)


def stable_threshold_decision(
    similarity: float,
    config: PrecisionConfig,
) -> dict[str, float | bool | str]:
    quantum = config.decision_quantum
    sim_q = stable_quantize(similarity, quantum)
    threshold_q = stable_quantize(config.threshold, quantum)
    distance = stable_round(sim_q - threshold_q, 12)
    within = abs(distance) <= max(config.tolerance, quantum / 2.0)
    if config.comparison_strategy == "strict":
        accepted = sim_q >= threshold_q
        relation = "at_or_above" if accepted else "below"
    else:
        accepted = (sim_q >= threshold_q) or within
        relation = "tolerance_band_accept" if within and sim_q < threshold_q else (
            "at_or_above" if accepted else "below"
        )
    return {
        "accepted": bool(accepted),
        "similarity_quantized": sim_q,
        "threshold_quantized": threshold_q,
        "distance": distance,
        "within_tolerance": bool(within),
        "relation": relation,
        "strategy": config.comparison_strategy,
    }


def profile_digest(payload: dict) -> str:
    return digest_payload(payload)
