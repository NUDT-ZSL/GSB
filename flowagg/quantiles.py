from __future__ import annotations

import math
from decimal import Decimal
from typing import Protocol

from .plan import AggregationPlan, fixed_quantile


class OrderValueSource(Protocol):
    def group_quantile_order_value(self, group_id: str, field: str, offset: int) -> int: ...


def _interpolation(
    lower: int,
    upper: int,
    fraction: Decimal,
) -> tuple[float, Decimal]:
    numerator = Decimal(upper) - Decimal(lower)
    raw_decimal = Decimal(lower) + numerator * fraction
    raw_float = float(lower) + float(fraction) * float(upper - lower)
    if not math.isfinite(raw_float):
        raw_float = float(raw_decimal)
    return raw_float, raw_decimal


def _trim_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def quantile_for_sorted_values(
    values: list[int],
    q: float,
    scale: int,
) -> dict[str, object]:
    if not values:
        raise ValueError("cannot calculate a quantile of an empty series")
    count = len(values)
    q_decimal = Decimal(str(q))
    h_decimal = Decimal(count - 1) * q_decimal
    lower_index = int(h_decimal.to_integral_value(rounding="ROUND_FLOOR"))
    upper_index = min(lower_index + 1, count - 1)
    fraction = h_decimal - Decimal(lower_index)
    lower = values[lower_index]
    upper = values[upper_index]
    raw_float, raw_decimal = _interpolation(lower, upper, fraction)
    fixed = fixed_quantile(raw_decimal, scale)
    return {
        "fixed": fixed,
        "fixed_meta": {
            "anchor_raw_values": [lower, upper],
            "interpolation": "linear_type7",
            "order_by": ["value", "clock_ms", "record_seq"],
            "raw_value": _trim_decimal(raw_decimal),
            "rounding": "half_up",
            "scale": scale,
        },
        "float": raw_float,
    }


def quantiles_for_group(
    source: OrderValueSource,
    group_id: str,
    count: int,
    plan: AggregationPlan,
) -> dict[str, dict[str, dict[str, object]]]:
    result: dict[str, dict[str, dict[str, object]]] = {"bytes": {}, "packets": {}}
    if count == 0:
        return result
    offsets: set[int] = set()
    selections: list[tuple[str, float, int, int, Decimal]] = []
    for q in plan.quantiles:
        q_decimal = Decimal(str(q))
        h_decimal = Decimal(count - 1) * q_decimal
        lower_index = int(h_decimal.to_integral_value(rounding="ROUND_FLOOR"))
        upper_index = min(lower_index + 1, count - 1)
        fraction = h_decimal - Decimal(lower_index)
        offsets.update((lower_index, upper_index))
        selections.append((str(q), q, lower_index, upper_index, fraction))

    cache: dict[tuple[str, int], int] = {}
    for field in ("bytes", "packets"):
        for offset in sorted(offsets):
            cache[(field, offset)] = source.group_quantile_order_value(
                group_id, field, offset
            )

    for field in ("bytes", "packets"):
        for q_text, _q, lower_index, upper_index, fraction in selections:
            lower = cache[(field, lower_index)]
            upper = cache[(field, upper_index)]
            raw_float, raw_decimal = _interpolation(lower, upper, fraction)
            fixed = fixed_quantile(raw_decimal, plan.fixed_scale)
            result[field][q_text] = {
                "fixed": fixed,
                "fixed_meta": {
                    "anchor_raw_values": [lower, upper],
                    "interpolation": "linear_type7",
                    "order_by": [field, "clock_ms", "record_seq"],
                    "raw_value": _trim_decimal(raw_decimal),
                    "rounding": "half_up",
                    "scale": plan.fixed_scale,
                },
                "float": raw_float,
            }
    return result

