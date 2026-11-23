"""Stable threshold comparison policies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import ThresholdPolicyInvalid, ToleranceInvalid

VALID_POLICIES = ("strict", "tolerance")
_TINY = float(np.finfo(np.float64).tiny)


def validate_threshold_policy(policy: str, field: str = "thresholds.policy") -> str:
    if policy not in VALID_POLICIES:
        raise ThresholdPolicyInvalid(field=field, policy=policy)
    return policy


def validate_tolerance(
    value: Any,
    field: str,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ToleranceInvalid(field=field, value=value) from exc
    if not math.isfinite(number) or number < 0.0:
        raise ToleranceInvalid(field=field, value=value)
    return number


def _finite_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ThresholdPolicyInvalid(field=field, policy=None) from exc
    if not math.isfinite(number):
        raise ThresholdPolicyInvalid(field=field, policy=None)
    return number


def _snap_subnormal(value: float, protect_subnormal: bool) -> tuple[float, bool]:
    if protect_subnormal and 0.0 < abs(value) < _TINY:
        return 0.0, True
    return value, False


@dataclass(frozen=True)
class ThresholdDecision:
    field: str
    policy: str
    direction: str
    metric: float
    threshold: float
    effective_tolerance: float
    passed: bool
    within_tolerance: bool
    subnormal_clamped_metric: bool
    subnormal_clamped_threshold: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "policy": self.policy,
            "direction": self.direction,
            "metric": self.metric,
            "threshold": self.threshold,
            "effective_tolerance": self.effective_tolerance,
            "passed": self.passed,
            "within_tolerance": self.within_tolerance,
            "subnormal_clamped_metric": self.subnormal_clamped_metric,
            "subnormal_clamped_threshold": self.subnormal_clamped_threshold,
        }


def evaluate_threshold(
    metric: Any,
    threshold: Any,
    *,
    policy: str,
    tolerance_abs: Any = 0.0,
    tolerance_rel: Any = 0.0,
    tolerance: Any | None = None,
    direction: str = "le",
    field: str = "threshold",
    protect_subnormal: bool = True,
) -> ThresholdDecision:
    """Compare a metric against a limit without boundary-condition flapping.

    ``strict`` uses the raw IEEE comparison. ``tolerance`` admits a symmetric
    absolute-relative band; metrics inside that band are treated as boundary
    values and therefore pass when they are within the tolerated side.
    """

    validate_threshold_policy(policy, field=f"{field}.policy")
    if tolerance is not None:
        tolerance_abs = tolerance
    abs_tol = validate_tolerance(tolerance_abs, f"{field}.tolerance_abs")
    rel_tol = validate_tolerance(tolerance_rel, f"{field}.tolerance_rel")
    if direction not in ("le", "ge"):
        raise ThresholdPolicyInvalid(field=f"{field}.direction", policy=direction)

    metric_value = _finite_number(metric, f"{field}.metric")
    threshold_value = _finite_number(threshold, f"{field}.threshold")
    metric_value, metric_clamped = _snap_subnormal(metric_value, protect_subnormal)
    threshold_value, threshold_clamped = _snap_subnormal(
        threshold_value, protect_subnormal
    )

    scale = max(abs(threshold_value), _TINY)
    effective_tolerance = abs_tol + rel_tol * scale
    within = abs(metric_value - threshold_value) <= effective_tolerance

    if policy == "strict":
        passed = (
            metric_value <= threshold_value
            if direction == "le"
            else metric_value >= threshold_value
        )
    else:
        passed = (
            metric_value <= threshold_value + effective_tolerance
            if direction == "le"
            else metric_value >= threshold_value - effective_tolerance
        )

    return ThresholdDecision(
        field=field,
        policy=policy,
        direction=direction,
        metric=metric_value,
        threshold=threshold_value,
        effective_tolerance=effective_tolerance,
        passed=bool(passed),
        within_tolerance=bool(within),
        subnormal_clamped_metric=metric_clamped,
        subnormal_clamped_threshold=threshold_clamped,
    )

