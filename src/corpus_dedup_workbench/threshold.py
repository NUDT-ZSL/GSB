from __future__ import annotations

import math
import sys
from decimal import Decimal
from fractions import Fraction
from typing import Any

from .util import decimal_text


def _fraction_from_decimal(value: Decimal) -> Fraction:
    return Fraction(value)


def stable_threshold_decision(
    matched: int,
    denominator: int,
    threshold: Decimal,
    policy: str,
    tolerance: Decimal,
) -> dict[str, Any]:
    if denominator <= 0:
        score_fraction = Fraction(0, 1)
    else:
        score_fraction = Fraction(matched, denominator)

    score_float = float(score_fraction)
    subnormal_guarded = False
    compare_fraction = score_fraction
    if score_float > 0 and score_float < sys.float_info.min:
        subnormal_guarded = True
        compare_fraction = Fraction(0, 1)

    threshold_fraction = _fraction_from_decimal(threshold)
    tolerance_fraction = _fraction_from_decimal(tolerance)
    absolute_difference = abs(compare_fraction - threshold_fraction)
    in_tolerance_band = absolute_difference <= tolerance_fraction
    decision = compare_fraction >= threshold_fraction
    rule = "exact_fraction_compare"
    if policy == "tolerance_band" and in_tolerance_band:
        rule = "exact_cross_product_in_tolerance_band"
    elif policy == "tolerance_band":
        rule = "exact_compare_outside_tolerance_band"

    return {
        "matched": matched,
        "denominator": denominator,
        "score": score_float,
        "score_fraction": f"{score_fraction.numerator}/{score_fraction.denominator}",
        "score_decimal": decimal_text(
            Decimal(score_fraction.numerator) / Decimal(score_fraction.denominator)
        ),
        "threshold": decimal_text(threshold),
        "threshold_policy": policy,
        "tolerance": decimal_text(tolerance),
        "absolute_difference": decimal_text(
            Decimal(absolute_difference.numerator)
            / Decimal(absolute_difference.denominator)
        ),
        "in_tolerance_band": in_tolerance_band,
        "subnormal_guarded": subnormal_guarded,
        "subnormal_float_min": sys.float_info.min,
        "decision": decision,
        "rule": rule,
    }


def subnormal_probe(score: float, threshold: Decimal) -> dict[str, Any]:
    """Diagnostic helper that shows the subnormal guard without changing inputs."""
    guarded = score > 0 and score < sys.float_info.min
    effective = 0.0 if guarded else score
    return {
        "score": score,
        "effective_score": effective,
        "guarded": guarded,
        "threshold": decimal_text(threshold),
        "decision": Fraction(effective) >= Fraction(threshold),
        "math_isfinite": math.isfinite(score),
    }

