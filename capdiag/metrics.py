from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any

from .aggregation import WindowAggregate
from .fixedpoint import (
    FixedMetricResult,
    convert_with_carry,
    decimal_to_fraction,
    fraction_decimal,
)


@dataclass(frozen=True)
class RawMetric:
    value: Fraction
    formula_ops: list[dict[str, Any]]


def _linear_raw(definition: dict[str, Any], aggregate: WindowAggregate) -> RawMetric:
    params = definition["params"]
    ops: list[dict[str, Any]] = []

    base = int(params.get("base_units", 0))
    count_coefficient = int(params.get("count_coefficient", 0))
    duration_coefficient = int(params.get("duration_coefficient", 0))
    success_coefficient = int(params.get("success_coefficient", 0))
    dropped_coefficient = int(params.get("dropped_coefficient", 0))
    denominator = int(params["denominator"])

    count_term = count_coefficient * aggregate.request_count
    ops.append(
        {
            "op": "multiply_count_coefficient",
            "left": count_coefficient,
            "right": aggregate.request_count,
            "output": count_term,
        }
    )

    duration_fraction = decimal_to_fraction(aggregate.total_duration_ms)
    duration_term = Fraction(duration_coefficient) * duration_fraction
    ops.append(
        {
            "op": "multiply_duration_coefficient",
            "left": duration_coefficient,
            "right_duration_ms": duration_fraction,
            "output": duration_term,
        }
    )

    success_term = success_coefficient * aggregate.success_count
    ops.append(
        {
            "op": "multiply_success_coefficient",
            "left": success_coefficient,
            "right": aggregate.success_count,
            "output": success_term,
        }
    )

    dropped_term = dropped_coefficient * aggregate.dropped_count
    ops.append(
        {
            "op": "multiply_dropped_coefficient",
            "left": dropped_coefficient,
            "right": aggregate.dropped_count,
            "output": dropped_term,
        }
    )

    numerator = Fraction(base)
    for name, term in (
        ("add_count_term", Fraction(count_term)),
        ("add_duration_term", duration_term),
        ("add_success_term", Fraction(success_term)),
        ("add_dropped_term", Fraction(dropped_term)),
    ):
        numerator += term
        ops.append({"op": name, "output": numerator})

    value = numerator / denominator
    ops.append(
        {
            "op": "formula_divide",
            "numerator": numerator,
            "denominator": denominator,
            "output": value,
            "rounding_direction": "exact_rational_retained",
        }
    )
    return RawMetric(value=value, formula_ops=ops)


def _p95_raw(definition: dict[str, Any], aggregate: WindowAggregate) -> RawMetric:
    if aggregate.request_count == 0:
        return RawMetric(Fraction(0), [{"op": "empty_window_p95", "output": 0}])
    ordered = sorted(aggregate.durations_ms)
    count = len(ordered)
    rank = (95 * count + 99) // 100
    selected = ordered[rank - 1]
    ops = [
        {
            "op": "p95_rank_multiply_and_ceil",
            "percent_numerator": 95,
            "request_count": count,
            "rank": rank,
            "ordered_position": rank - 1,
        },
        {
            "op": "p95_select_duration",
            "selected_duration_ms": selected,
            "output": decimal_to_fraction(selected),
        },
    ]
    return RawMetric(decimal_to_fraction(selected), ops)


def _throughput_raw(
    definition: dict[str, Any],
    aggregate: WindowAggregate,
    expected_request_rate: Decimal,
) -> RawMetric:
    length_ms = int(definition["window_length_ms"])
    seconds = Fraction(length_ms, 1000)
    rate = decimal_to_fraction(expected_request_rate)
    expected = rate * seconds
    ops = [
        {
            "op": "window_seconds_divide",
            "left_ms": length_ms,
            "divisor": 1000,
            "output": seconds,
            "rounding_direction": "exact_rational_retained",
        },
        {
            "op": "expected_requests_multiply_rate_seconds",
            "rate": rate,
            "seconds": seconds,
            "output": expected,
        },
    ]
    if expected == 0:
        value = Fraction(0)
    else:
        value = Fraction(aggregate.success_count) / expected
    ops.append(
        {
            "op": "throughput_divide_success_by_expected",
            "success_count": aggregate.success_count,
            "expected_requests": expected,
            "output": value,
            "rounding_direction": "exact_rational_retained",
        }
    )
    return RawMetric(value, ops)


def raw_metric(
    metric_name: str,
    definition: dict[str, Any],
    aggregate: WindowAggregate,
    expected_request_rate: Decimal,
) -> RawMetric:
    kind = definition["kind"]
    if kind == "linear_ratio":
        return _linear_raw(definition, aggregate)
    if kind == "latency_p95":
        return _p95_raw(definition, aggregate)
    if kind == "throughput_ratio":
        return _throughput_raw(definition, aggregate, expected_request_rate)
    raise ValueError(f"unsupported metric kind: {kind}")


def calculate_fixed_metric(
    metric_name: str,
    definition: dict[str, Any],
    aggregate: WindowAggregate,
    expected_request_rate: Decimal,
    carry_in: Fraction,
) -> FixedMetricResult:
    raw = raw_metric(metric_name, definition, aggregate, expected_request_rate)
    scale = int(definition["scale"])
    return convert_with_carry(
        metric=metric_name,
        scale=scale,
        raw_value=raw.value,
        carry_in=carry_in,
        formula_ops=raw.formula_ops,
    )


def decimal_truth_metric(
    metric_name: str,
    definition: dict[str, Any],
    aggregate: WindowAggregate,
    expected_request_rate: Decimal,
) -> Decimal:
    """Independent Decimal high-precision recomputation in formula order."""
    with localcontext() as ctx:
        ctx.prec = 80
        kind = definition["kind"]
        if kind == "linear_ratio":
            params = definition["params"]
            numerator = Decimal(int(params.get("base_units", 0)))
            numerator += Decimal(int(params.get("count_coefficient", 0))) * Decimal(
                aggregate.request_count
            )
            numerator += Decimal(int(params.get("duration_coefficient", 0))) * aggregate.total_duration_ms
            numerator += Decimal(int(params.get("success_coefficient", 0))) * Decimal(
                aggregate.success_count
            )
            numerator += Decimal(int(params.get("dropped_coefficient", 0))) * Decimal(
                aggregate.dropped_count
            )
            return numerator / Decimal(int(params["denominator"]))

        if kind == "latency_p95":
            if not aggregate.durations_ms:
                return Decimal(0)
            ordered = sorted(aggregate.durations_ms)
            count = len(ordered)
            rank = (95 * count + 99) // 100
            return ordered[rank - 1]

        length_seconds = Decimal(int(definition["window_length_ms"])) / Decimal(1000)
        expected = expected_request_rate * length_seconds
        if expected == 0:
            return Decimal(0)
        return Decimal(aggregate.success_count) / expected


def compare_values(
    *,
    fixed_value: int,
    scale: int,
    truth: Decimal,
    error_bound: Fraction,
) -> dict[str, Any]:
    fixed_decimal = Decimal(fixed_value) / Decimal(scale)
    absolute_deviation = abs(fixed_decimal - truth)
    relative_deviation = (
        absolute_deviation / abs(truth) if truth != 0 else absolute_deviation
    )
    bound_decimal = fraction_decimal(error_bound)
    precision_epsilon = Decimal("1e-70")
    within_budget = absolute_deviation <= bound_decimal + precision_epsilon
    return {
        "fixed_decimal": format(fixed_decimal, "f"),
        "truth_decimal": format(truth, "f"),
        "absolute_deviation": format(absolute_deviation, "f"),
        "relative_deviation": format(relative_deviation, "f"),
        "error_bound": format(bound_decimal, "f"),
        "within_error_budget": within_budget,
    }
