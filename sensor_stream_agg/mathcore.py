from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Iterable, Sequence

from .compensation import UNIT_ROUNDOFF, AccumulatorState


@dataclass(frozen=True, slots=True)
class ReferenceValues:
    sum_value: float
    rms_value: float
    weighted_mean: float


@dataclass(frozen=True, slots=True)
class Snapshot:
    member_count: int
    sum_state: AccumulatorState
    weighted_sum_state: AccumulatorState
    weight_state: AccumulatorState
    square_sum_state: AccumulatorState
    sum_value: float
    sum_error_bound: float
    rms_value: float
    rms_error_bound: float
    weighted_mean: float
    weighted_mean_error_bound: float


def canonical_rows(
    rows: Iterable[tuple[int, int, int, float, float]],
) -> list[tuple[float, float]]:
    """Sort by (event_time, device, sequence) and return (value, weight)."""

    ordered = sorted(rows, key=lambda row: (row[0], row[1], row[2]))
    return [(float(row[3]), float(row[4])) for row in ordered]


def _rms(square_state: AccumulatorState) -> tuple[float, float]:
    if square_state.count == 0:
        return 0.0, 0.0
    count = square_state.count
    mean_square = square_state.value / count
    if mean_square < 0.0 and mean_square > -10.0 * UNIT_ROUNDOFF:
        mean_square = 0.0
    rms_value = math.sqrt(max(0.0, mean_square))
    mean_square_bound = (
        square_state.error_bound(UNIT_ROUNDOFF) / count
        + UNIT_ROUNDOFF * abs(mean_square)
    )
    if math.isinf(mean_square_bound):
        return rms_value, math.inf
    # |sqrt(a) - sqrt(b)| <= sqrt(|a-b|) for non-negative a, b. Add the final
    # square-root rounding contribution.
    return rms_value, math.sqrt(mean_square_bound) + UNIT_ROUNDOFF * abs(rms_value)


def _weighted_mean(
    weighted_sum_state: AccumulatorState,
    weight_state: AccumulatorState,
) -> tuple[float, float]:
    weight = weight_state.value
    if weight <= 0.0 or not math.isfinite(weight):
        return math.nan, math.inf
    weighted = weighted_sum_state.value
    value = weighted / weight
    weighted_error = weighted_sum_state.error_bound(UNIT_ROUNDOFF)
    weight_error = weight_state.error_bound()
    denominator = abs(weight) - weight_error
    if denominator <= 0.0:
        return value, math.inf
    propagated = (
        weighted_error * (abs(weight) + 2.0 * weight_error)
        + abs(weighted) * weight_error
    ) / (abs(weight) * denominator)
    return value, propagated + UNIT_ROUNDOFF * abs(value)


def compute_snapshot(rows: Iterable[tuple[int, int, int, float, float]]) -> Snapshot:
    canonical = canonical_rows(rows)
    sum_state = AccumulatorState.from_terms(value for value, _ in canonical)
    weighted_sum_state = AccumulatorState.from_terms(
        value * weight for value, weight in canonical
    )
    weight_state = AccumulatorState.from_terms(weight for _, weight in canonical)
    square_sum_state = AccumulatorState.from_terms(
        value * value for value, _ in canonical
    )
    rms_value, rms_error = _rms(square_sum_state)
    weighted_mean, weighted_mean_error = _weighted_mean(
        weighted_sum_state, weight_state
    )
    return Snapshot(
        member_count=len(canonical),
        sum_state=sum_state,
        weighted_sum_state=weighted_sum_state,
        weight_state=weight_state,
        square_sum_state=square_sum_state,
        sum_value=sum_state.value,
        sum_error_bound=sum_state.error_bound(),
        rms_value=rms_value,
        rms_error_bound=rms_error,
        weighted_mean=weighted_mean,
        weighted_mean_error_bound=weighted_mean_error,
    )


def reference_values(
    rows: Sequence[tuple[int, int, int, float, float]],
) -> ReferenceValues:
    """High-precision reference using Decimal exact conversion of binary64."""

    canonical = canonical_rows(rows)
    if not canonical:
        return ReferenceValues(0.0, 0.0, math.nan)
    with localcontext() as context:
        context.prec = 100
        values = [Decimal.from_float(value) for value, _ in canonical]
        weights = [Decimal.from_float(weight) for _, weight in canonical]
        total = sum(values, Decimal(0))
        square_total = sum((value * value for value in values), Decimal(0))
        weighted_total = sum(
            (value * weight for value, weight in zip(values, weights)), Decimal(0)
        )
        weight_total = sum(weights, Decimal(0))
        rms = (square_total / Decimal(len(values))).sqrt()
        weighted_mean = weighted_total / weight_total
        return ReferenceValues(float(total), float(rms), float(weighted_mean))


def bound_failures(
    snapshot: Snapshot,
    reference: ReferenceValues,
) -> tuple[float, float, float, tuple[str, ...]]:
    sum_deviation = abs(snapshot.sum_value - reference.sum_value)
    rms_deviation = abs(snapshot.rms_value - reference.rms_value)
    weighted_mean_deviation = abs(snapshot.weighted_mean - reference.weighted_mean)
    failures: list[str] = []
    if not sum_deviation <= snapshot.sum_error_bound:
        failures.append("sum")
    if not rms_deviation <= snapshot.rms_error_bound:
        failures.append("rms")
    if not weighted_mean_deviation <= snapshot.weighted_mean_error_bound:
        failures.append("weighted_mean")
    return sum_deviation, rms_deviation, weighted_mean_deviation, tuple(failures)

