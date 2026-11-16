"""Public error-budget and burn-rate calculations with bounded output."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .errors import ConfigError
from .fixed import FixedPoint, quantize


@dataclass(frozen=True, slots=True)
class BudgetMetrics:
    sli: FixedPoint
    error_rate: FixedPoint
    budget_fraction: FixedPoint
    budget_total: FixedPoint
    budget_consumed: FixedPoint
    budget_remaining: FixedPoint
    burn_rate: FixedPoint
    exact_sli: Fraction | None
    exact_burn_rate: Fraction | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sli": self.sli.to_dict(),
            "error_rate": self.error_rate.to_dict(),
            "budget_fraction": self.budget_fraction.to_dict(),
            "budget_total": self.budget_total.to_dict(),
            "budget_consumed": self.budget_consumed.to_dict(),
            "budget_remaining": self.budget_remaining.to_dict(),
            "burn_rate": self.burn_rate.to_dict(),
        }


def _validate_inputs(total_requests: int, failed_requests: int, target_ppm: int) -> None:
    for name, value in (
        ("total_requests", total_requests),
        ("failed_requests", failed_requests),
        ("target_ppm", target_ppm),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{name} must be an integer")
    if total_requests < 0:
        raise ConfigError("total_requests cannot be negative")
    if failed_requests < 0 or failed_requests > total_requests:
        raise ConfigError("failed_requests must satisfy 0 <= failed_requests <= total_requests")
    if not 0 < target_ppm < 1_000_000:
        raise ConfigError("target_ppm must satisfy 0 < target_ppm < 1000000")


def compute_budget_metrics(
    total_requests: int,
    failed_requests: int,
    target_ppm: int,
    *,
    ratio_scale: int = 1_000_000_000,
    burn_scale: int = 1_000_000,
    request_scale: int = 1_000_000,
) -> BudgetMetrics:
    """Compute SLI, error budget, and burn rate from exact integer counts."""

    _validate_inputs(total_requests, failed_requests, target_ppm)
    budget_fraction_exact = Fraction(1_000_000 - target_ppm, 1_000_000)
    if total_requests > 0:
        exact_sli: Fraction | None = Fraction(total_requests - failed_requests, total_requests)
        exact_error_rate: Fraction | None = Fraction(failed_requests, total_requests)
        exact_burn: Fraction | None = exact_error_rate / budget_fraction_exact
        sli = quantize(exact_sli, ratio_scale)
        error_rate = quantize(exact_error_rate, ratio_scale)
        burn_rate = quantize(exact_burn, burn_scale)
    else:
        exact_sli = None
        exact_burn = None
        sli = FixedPoint.undefined(ratio_scale)
        error_rate = FixedPoint.undefined(ratio_scale)
        burn_rate = FixedPoint.undefined(burn_scale)

    allowed_failures = Fraction(total_requests * (1_000_000 - target_ppm), 1_000_000)
    remaining = allowed_failures - failed_requests
    return BudgetMetrics(
        sli=sli,
        error_rate=error_rate,
        budget_fraction=quantize(budget_fraction_exact, ratio_scale),
        budget_total=quantize(allowed_failures, request_scale),
        budget_consumed=quantize(Fraction(failed_requests, 1), request_scale),
        budget_remaining=quantize(remaining, request_scale),
        burn_rate=burn_rate,
        exact_sli=exact_sli,
        exact_burn_rate=exact_burn,
    )


def compute_burn_rate(
    total_requests: int,
    failed_requests: int,
    target_ppm: int,
    *,
    burn_scale: int = 1_000_000,
) -> FixedPoint:
    """Return only the fixed-point burn rate, including its error upper bound."""

    return compute_budget_metrics(
        total_requests,
        failed_requests,
        target_ppm,
        burn_scale=burn_scale,
    ).burn_rate
