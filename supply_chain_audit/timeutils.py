"""Observation-clock based second-to-millisecond conversion."""

from __future__ import annotations

from typing import Any

from .codes import E_NON_NEGATIVE_INT


def is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def convert_relative_seconds(seconds: int, clock: dict[str, Any]) -> dict[str, Any]:
    """Convert integer seconds to a bounded millisecond interval.

    The canonical point is the inclusive lower endpoint.  A source second can
    represent any millisecond in ``[lower, upper]``; therefore the lower
    endpoint has a maximum error of 999 ms while the midpoint has a symmetric
    bound of 499.5 ms.
    """

    origin_ms = clock["relative_second_origin_ms"]
    lower_ms = origin_ms + seconds * 1000
    upper_ms = lower_ms + 999
    return {
        "source": "v1_relative_seconds",
        "clock_id": clock.get("clock_id"),
        "relative_seconds": seconds,
        "origin_ms": origin_ms,
        "absolute_ms": lower_ms,
        "lower_endpoint_ms": lower_ms,
        "upper_endpoint_ms": upper_ms,
        "endpoint_span_ms": 1000,
        "error_bound_ms": 999,
        "midpoint_ms": lower_ms + 499.5,
        "midpoint_error_bound_ms": 499.5,
    }


def millisecond_in_second_interval(value_ms: int, conversion: dict[str, Any]) -> bool:
    return conversion["lower_endpoint_ms"] <= value_ms <= conversion["upper_endpoint_ms"]


def validate_clock(clock: Any) -> list[dict[str, Any]]:
    errors = []
    if not isinstance(clock, dict):
        return [{"code": "E_CLOCK_SCHEMA", "message": "clock declaration must be an object"}]
    for field in ("observed_at_ms", "relative_second_origin_ms"):
        if not is_non_negative_int(clock.get(field)):
            errors.append(
                {
                    "code": E_NON_NEGATIVE_INT,
                    "field": field,
                    "message": f"{field} must be a non-negative integer",
                }
            )
    return errors
