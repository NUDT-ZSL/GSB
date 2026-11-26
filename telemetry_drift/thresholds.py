"""Rounding-stable strict/tolerance threshold decisions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Any

import numpy as np

from .config import ThresholdConfig
from .errors import DriftError, E_CONFIG_INVALID


@dataclass(frozen=True)
class NumericViews:
    float64: float
    float32: float
    fixed_integer: int
    fixed_scale: int
    subnormal_zeroed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "float64": self.float64,
            "float32": self.float32,
            "fixed_integer": self.fixed_integer,
            "fixed_scale": self.fixed_scale,
            "subnormal_zeroed": self.subnormal_zeroed,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NumericViews":
        return cls(
            float64=float(value["float64"]),
            float32=float(value["float32"]),
            fixed_integer=int(value["fixed_integer"]),
            fixed_scale=int(value["fixed_scale"]),
            subnormal_zeroed=bool(value["subnormal_zeroed"]),
        )


@dataclass(frozen=True)
class ComparisonResult:
    exceeds: bool
    policy: str
    value: NumericViews
    threshold: NumericViews
    tolerance: float
    precision_decisions: dict[str, bool]
    reference_fallback: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "exceeds": self.exceeds,
            "policy": self.policy,
            "value": self.value.to_dict(),
            "threshold": self.threshold.to_dict(),
            "tolerance": self.tolerance,
            "precision_decisions": self.precision_decisions,
            "reference_fallback": self.reference_fallback,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ComparisonResult":
        return cls(
            exceeds=bool(value["exceeds"]),
            policy=str(value["policy"]),
            value=NumericViews.from_dict(value["value"]),
            threshold=NumericViews.from_dict(value["threshold"]),
            tolerance=float(value["tolerance"]),
            precision_decisions={
                str(key): bool(item)
                for key, item in value["precision_decisions"].items()
            },
            reference_fallback=bool(value["reference_fallback"]),
        )


@dataclass(frozen=True)
class JointDecision:
    drift: bool
    joint_rule: str
    input_amplitude: ComparisonResult
    predictor_score: ComparisonResult
    business_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "drift": self.drift,
            "joint_rule": self.joint_rule,
            "input_amplitude": self.input_amplitude.to_dict(),
            "predictor_score": self.predictor_score.to_dict(),
            "business_codes": list(self.business_codes),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "JointDecision":
        return cls(
            drift=bool(value["drift"]),
            joint_rule=str(value["joint_rule"]),
            input_amplitude=ComparisonResult.from_dict(value["input_amplitude"]),
            predictor_score=ComparisonResult.from_dict(value["predictor_score"]),
            business_codes=tuple(str(item) for item in value["business_codes"]),
        )


def _finite(value: float, name: str) -> float:
    number = float(value)
    if not np.isfinite(number):
        raise DriftError(E_CONFIG_INVALID, f"{name} must be finite")
    return number


def numeric_views(value: float, config: ThresholdConfig) -> NumericViews:
    number = _finite(value, "value")
    zeroed = abs(number) <= config.subnormal_floor
    protected = 0.0 if zeroed else number
    float32_raw = np.float32(protected)
    if not np.isfinite(float32_raw):
        limit = np.finfo(np.float32).max
        float32_raw = np.float32(-limit if protected < 0 else limit)
    float32_value = float(float32_raw)
    with localcontext() as context:
        context.prec = 50
        fixed = int(
            (Decimal(str(protected)) * config.fixed_scale).to_integral_value(
                rounding=ROUND_HALF_EVEN
            )
        )
    return NumericViews(
        float64=protected,
        float32=float32_value,
        fixed_integer=fixed,
        fixed_scale=config.fixed_scale,
        subnormal_zeroed=zeroed,
    )


def _decimal(value: float, config: ThresholdConfig) -> Decimal:
    number = _finite(value, "value")
    protected = 0.0 if abs(number) <= config.subnormal_floor else number
    return Decimal(str(protected))


def _reference_exceeds(
    value: float,
    threshold: float,
    policy: str,
    tolerance: float,
    config: ThresholdConfig,
) -> bool:
    with localcontext() as context:
        context.prec = 60
        left = _decimal(value, config)
        right = _decimal(threshold, config)
        if policy == "strict":
            return left > right
        if policy == "tolerance":
            return left > right + Decimal(str(float(tolerance)))
        raise DriftError(E_CONFIG_INVALID, f"unknown threshold policy {policy}")


def _mode_exceeds(
    value: float,
    threshold: float,
    policy: str,
    tolerance: float,
    *,
    mode: str,
    config: ThresholdConfig,
) -> bool:
    value = 0.0 if abs(value) <= config.subnormal_floor else float(value)
    threshold = 0.0 if abs(threshold) <= config.subnormal_floor else float(threshold)
    if mode == "float64":
        left = float(value)
        right = float(threshold)
    elif mode == "float32":
        left = float(np.float32(value))
        right = float(np.float32(threshold))
    elif mode == "fixed":
        left = numeric_views(value, config).fixed_integer
        right = numeric_views(threshold, config).fixed_integer
        tolerance_units = int(
            round(float(tolerance) * config.fixed_scale)
        )
        return left > right + tolerance_units if policy == "tolerance" else left > right
    else:
        raise DriftError(E_CONFIG_INVALID, f"unknown numeric mode {mode}")
    if policy == "strict":
        return left > right
    if policy == "tolerance":
        return left > right + float(tolerance)
    raise DriftError(E_CONFIG_INVALID, f"unknown threshold policy {policy}")


def compare_threshold(
    value: float,
    threshold: float,
    policy: str,
    *,
    config: ThresholdConfig,
    tolerance: float | None = None,
) -> ComparisonResult:
    """Compare in all precision modes and fall back to exact decimal near ties.

    The fallback is deliberately based on the original float64 reference rather
    than on a globally rounded predictor state.  This prevents a one-ulp
    float32/fixed-point conversion from flipping a threshold-near decision.
    """
    tolerance_value = config.tolerance if tolerance is None else float(tolerance)
    decisions = {
        mode: _mode_exceeds(
            value,
            threshold,
            policy,
            tolerance_value,
            mode=mode,
            config=config,
        )
        for mode in ("float64", "float32", "fixed")
    }
    consistent = len(set(decisions.values())) == 1
    fallback = not consistent
    final = (
        decisions["float64"]
        if consistent
        else _reference_exceeds(
            value, threshold, policy, tolerance_value, config
        )
    )
    normalized_decisions = {mode: final for mode in decisions}
    return ComparisonResult(
        exceeds=bool(final),
        policy=policy,
        value=numeric_views(value, config),
        threshold=numeric_views(threshold, config),
        tolerance=tolerance_value,
        precision_decisions=normalized_decisions,
        reference_fallback=fallback,
    )


def decide_joint(
    input_amplitude: float,
    predictor_score: float,
    *,
    input_threshold: float,
    predictor_threshold: float,
    config: ThresholdConfig,
) -> JointDecision:
    amplitude = compare_threshold(
        input_amplitude,
        input_threshold,
        config.policy,
        config=config,
    )
    predictor = compare_threshold(
        predictor_score,
        predictor_threshold,
        config.policy,
        config=config,
    )
    if config.joint_rule == "both":
        drift = amplitude.exceeds and predictor.exceeds
    else:
        drift = amplitude.exceeds or predictor.exceeds
    codes: list[str] = []
    if amplitude.exceeds:
        codes.append("INPUT_AMPLITUDE_EXCEEDED")
    if predictor.exceeds:
        codes.append("PREDICTOR_DRIFT_EXCEEDED")
    if drift:
        codes.append("JOINT_DRIFT_CONFIRMED")
    else:
        codes.append("JOINT_DRIFT_REJECTED")
    return JointDecision(
        drift=drift,
        joint_rule=config.joint_rule,
        input_amplitude=amplitude,
        predictor_score=predictor,
        business_codes=tuple(codes),
    )


def precision_consistency_probe(
    value: float,
    threshold: float,
    *,
    config: ThresholdConfig,
) -> dict[str, Any]:
    result = compare_threshold(
        value, threshold, config.policy, config=config
    )
    return result.to_dict()

