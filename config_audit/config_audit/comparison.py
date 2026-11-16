"""Consistency / deviation judgement on the integer grid.

Two values are compared using *both* an absolute and a relative tolerance.
Every quantity is an integer number of grid steps, so no floating point is
involved.

Boundary rule (explicit, inclusive): a pair is consistent when the deviation is
**less than or equal to** the effective tolerance.  The effective tolerance is
the *maximum* of the absolute tolerance and the relative tolerance
(``relative * max(|a|, |b|)``).  Whichever tolerance actually dominates is
reported back as ``triggered_tolerance``.

Each value also carries a propagated error interval.  When the deviation is no
larger than the combined uncertainty of the pair, the mathematical difference
cannot be distinguished from accumulated quantization/propagation error: the
decision is then flagged ``error_bound_dominated = true`` and the semantic
verdict is ``indistinguishable`` rather than a plain consistent/deviating call.
"""

from __future__ import annotations

from dataclasses import dataclass

from .fixedpoint import Grid, quantize
from .models import QuantizedItem

CONSISTENT = "consistent"
DEVIATING = "deviating"
INDISTINGUISHABLE = "indistinguishable"

TOL_ABS = "absolute"
TOL_REL = "relative"


@dataclass(frozen=True)
class Tolerance:
    abs_steps: int
    rel_ppm: int          # relative tolerance in parts-per-million grid integers
    rel_fraction_num: int
    rel_fraction_den: int

    @classmethod
    def create(cls, grid: Grid, abs_tolerance: str,
               relative_ppm: int) -> "Tolerance":
        abs_q = quantize(_to_decimal(abs_tolerance), grid, "abs_tolerance").quantized
        if abs_q < 0:
            raise ValueError("absolute tolerance must be non-negative")
        if not isinstance(relative_ppm, int) or isinstance(relative_ppm, bool):
            raise ValueError("relative_ppm must be an integer")
        if relative_ppm < 0:
            raise ValueError("relative_ppm must be non-negative")
        return cls(
            abs_steps=abs_q,
            rel_ppm=relative_ppm,
            rel_fraction_num=relative_ppm,
            rel_fraction_den=1_000_000,
        )


def _to_decimal(raw):
    from decimal import Decimal
    if isinstance(raw, Decimal):
        return raw
    return Decimal(str(raw))


def _ceil_div(num: int, den: int) -> int:
    # Nonnegative numerator, positive denominator.
    return -((-num) // den)


@dataclass(frozen=True)
class PairVerdict:
    node_a: str
    node_b: str
    key: str
    value_a: int
    value_b: int
    deviation: int
    abs_tolerance: int
    rel_tolerance: int
    effective_tolerance: int
    triggered_tolerance: str
    within_tolerance: bool
    uncertainty: int
    error_bound_dominated: bool
    verdict: str


def compare_pair(a: QuantizedItem, b: QuantizedItem, tol: Tolerance) -> PairVerdict:
    deviation = abs(a.qvalue - b.qvalue)

    rel_raw = tol.rel_fraction_num * max(abs(a.qvalue), abs(b.qvalue))
    rel_tol = _ceil_div(rel_raw, tol.rel_fraction_den)
    effective = max(tol.abs_steps, rel_tol)
    # Deterministic rule: relative dominates only when strictly larger; on a
    # tie the absolute tolerance wins.
    triggered = TOL_REL if rel_tol > tol.abs_steps else TOL_ABS

    within = deviation <= effective  # inclusive boundary rule

    uncertainty = (max(a.down_error, a.up_error)
                   + max(b.down_error, b.up_error))
    # Error-bound dominance requires *real* uncertainty.  An exact, zero-error
    # match is genuinely consistent rather than indistinguishable.
    dominated = uncertainty > 0 and deviation <= uncertainty

    if dominated:
        verdict = INDISTINGUISHABLE
    elif within:
        verdict = CONSISTENT
    else:
        verdict = DEVIATING

    return PairVerdict(
        node_a=a.node_id,
        node_b=b.node_id,
        key=a.key,
        value_a=a.qvalue,
        value_b=b.qvalue,
        deviation=deviation,
        abs_tolerance=tol.abs_steps,
        rel_tolerance=rel_tol,
        effective_tolerance=effective,
        triggered_tolerance=triggered,
        within_tolerance=within,
        uncertainty=uncertainty,
        error_bound_dominated=dominated,
        verdict=verdict,
    )
