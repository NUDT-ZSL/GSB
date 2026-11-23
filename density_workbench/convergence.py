from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .errors import E_CONFIG_INVALID, BusinessError


@dataclass(frozen=True)
class ConvergenceDecision:
    iteration: int
    improvement: float
    tolerance: float
    min_improvement: float
    policy: str
    band: float
    effective_threshold: float
    protected: bool
    converged: bool
    reason: str
    comparison: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "improvement": self.improvement,
            "tolerance": self.tolerance,
            "min_improvement": self.min_improvement,
            "policy": self.policy,
            "band": self.band,
            "effective_threshold": self.effective_threshold,
            "protected": self.protected,
            "converged": self.converged,
            "reason": self.reason,
            "comparison": self.comparison,
        }


def validate_convergence_config(
    tol: float,
    min_improvement: float,
    rel_band: float,
    abs_band: float,
) -> None:
    for field_name, value, allow_zero in (
        ("tol", tol, False),
        ("min_improvement", min_improvement, True),
        ("convergence_rel_band", rel_band, True),
        ("convergence_abs_band", abs_band, True),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0.0
            or (not allow_zero and float(value) == 0.0)
        ):
            raise BusinessError(
                code=E_CONFIG_INVALID,
                message=f"invalid config field {field_name}",
                details={
                    "field": field_name,
                    "value": value,
                    "requirement": (
                        "finite and > 0" if not allow_zero else "finite and >= 0"
                    ),
                },
            )


def _canonical_threshold(value: float) -> float:
    """Map very close floating-point values to one comparison lattice.

    Fifteen significant digits are well above float32 resolution while below
    binary64 noise for ordinary tolerances. It prevents a value that is
    mathematically on a threshold from flipping solely because 0.1 or 1/3 was
    represented in a different precision.
    """

    if value == 0.0 or not math.isfinite(value):
        return value
    return float(f"{value:.15g}")


def decide_convergence(
    *,
    iteration: int,
    improvement: float,
    tol: float,
    min_improvement: float,
    policy: str,
    rel_band: float,
    abs_band: float,
    subnormal_floor: float = 0.0,
    numerical_epsilon: float = 1.1920928955078125e-07,
) -> ConvergenceDecision:
    validate_convergence_config(tol, min_improvement, rel_band, abs_band)
    if policy not in ("strict", "tolerance_band"):
        raise BusinessError(
            code=E_CONFIG_INVALID,
            message="invalid config field convergence_policy",
            details={
                "field": "convergence_policy",
                "value": policy,
                "allowed": ["strict", "tolerance_band"],
            },
        )
    if not math.isfinite(improvement):
        raise BusinessError(
            code=E_CONFIG_INVALID,
            message="non-finite log-likelihood improvement",
            details={"field": "log_likelihood", "improvement": improvement},
        )
    effective_threshold = max(float(tol), float(min_improvement))
    if not math.isfinite(float(numerical_epsilon)) or numerical_epsilon < 0.0:
        raise BusinessError(
            code=E_CONFIG_INVALID,
            message="invalid numerical_epsilon",
            details={
                "field": "numerical_epsilon",
                "value": numerical_epsilon,
                "requirement": "finite and >= 0",
            },
        )
    # Include a precision guard so a comparison that is numerically at the
    # threshold cannot flip between binary64, binary32 and fixed-point runs.
    precision_guard = (
        8.0
        * float(numerical_epsilon)
        * max(1.0, abs(float(effective_threshold)))
    )
    band = max(
        float(abs_band),
        abs(float(tol)) * float(rel_band),
        precision_guard,
    )
    floor = max(float(subnormal_floor), 0.0)
    canonical_improvement = _canonical_threshold(float(improvement))
    canonical_threshold = _canonical_threshold(effective_threshold)
    near_threshold = abs(canonical_improvement - canonical_threshold) <= band
    subnormal_protected = floor > 0.0 and abs(canonical_improvement) < floor
    protected = bool(near_threshold or subnormal_protected)

    if protected:
        # A protected comparison is deliberately resolved in the conservative
        # direction (stop) for both policies. This is deterministic across all
        # precision modes and is emitted as a protected round.
        converged = True
        reason = "protected_threshold_band"
        if subnormal_protected:
            reason = "protected_subnormal"
    elif policy == "strict":
        converged = canonical_improvement <= canonical_threshold
        reason = "strict_comparison"
    else:
        converged = canonical_improvement <= canonical_threshold + band
        reason = "tolerance_band_comparison"

    return ConvergenceDecision(
        iteration=iteration,
        improvement=float(improvement),
        tolerance=float(tol),
        min_improvement=float(min_improvement),
        policy=policy,
        band=band,
        effective_threshold=effective_threshold,
        protected=protected,
        converged=bool(converged),
        reason=reason,
        comparison={
            "canonical_improvement": canonical_improvement,
            "canonical_threshold": canonical_threshold,
            "near_threshold": near_threshold,
            "subnormal_protected": subnormal_protected,
            "numerical_epsilon": float(numerical_epsilon),
            "precision_guard": precision_guard,
        },
    )
