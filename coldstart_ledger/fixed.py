from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext


@dataclass(frozen=True)
class FixedResult:
    ticks: int
    remainder: int
    carry_in: int
    carry_out: int
    effective_numerator: int
    denominator: int
    scale: int
    direction: str


def convert_floor(
    base_numerator: int,
    denominator: int,
    scale: int,
    carry: int = 0,
) -> FixedResult:
    """Convert a rational quantity to fixed ticks, carrying sub-tick remainder.

    The stored unit is ``base_numerator / denominator``.  ``scale`` is the
    number of fixed ticks per unit.  The effective numerator includes the
    caller's sub-tick remainder exactly once.
    """
    if base_numerator < 0 or denominator <= 0 or scale <= 0 or carry < 0:
        raise ValueError("fixed conversion requires non-negative input and positive denominator/scale")
    effective = base_numerator * scale + carry
    ticks, remainder = divmod(effective, denominator)
    return FixedResult(
        ticks=ticks,
        remainder=remainder,
        carry_in=carry,
        carry_out=remainder,
        effective_numerator=effective,
        denominator=denominator,
        scale=scale,
        direction="floor",
    )


def decimal_ticks(
    base_numerator: int,
    denominator: int,
    scale: int,
    precision: int = 80,
) -> Decimal:
    if base_numerator < 0 or denominator <= 0 or scale <= 0:
        raise ValueError("decimal conversion requires non-negative input and positive denominator/scale")
    with localcontext() as context:
        context.prec = precision
        return (Decimal(base_numerator) * Decimal(scale)) / Decimal(denominator)


def canonical_decimal(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(value.quantize(Decimal(1)))
    return format(value.normalize(), "f")


def residual_key(scope_type: str, scope_id: str, metric: str, scale: int) -> str:
    return f"{scope_type}:{scope_id}:{metric}:{scale}"

