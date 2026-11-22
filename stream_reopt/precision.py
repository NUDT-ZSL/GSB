from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np


FLOAT_EPS = float(np.finfo(np.float64).eps)


def value_sort_key(value: int | float | str) -> tuple[str, Any]:
    if isinstance(value, bool):
        return ("i", int(value))
    if isinstance(value, int):
        return ("i", value)
    if isinstance(value, float):
        if math.isnan(value):
            return ("f", "nan")
        return ("f", value)
    return ("s", str(value))


def row_sort_key(row: dict[str, Any]) -> tuple[tuple[str, tuple[str, Any]], ...]:
    return tuple((name, value_sort_key(row[name])) for name in sorted(row))


@dataclass
class ErrorTracker:
    steps: list[dict[str, Any]] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)
    first_arrival: int | None = None
    last_arrival: int | None = None
    total_bound: float = 0.0

    def add(
        self,
        operation: str,
        result: float | int,
        bound: float,
        participating_tables: Iterable[str],
        batch_interval: tuple[int, int] | None,
        **detail: Any,
    ) -> None:
        safe_bound = max(0.0, float(bound))
        self.steps.append(
            {
                "operation": operation,
                "result": float(result),
                "error_bound": safe_bound,
                "participating_tables": sorted(set(participating_tables)),
                "batch_interval": list(batch_interval) if batch_interval else None,
                "detail": detail,
            }
        )
        self.sources.update(participating_tables)
        if batch_interval:
            first, last = batch_interval
            self.first_arrival = first if self.first_arrival is None else min(self.first_arrival, first)
            self.last_arrival = last if self.last_arrival is None else max(self.last_arrival, last)
        self.total_bound = math.fsum([self.total_bound, safe_bound])

    def to_chain(self) -> dict[str, Any]:
        return {
            "accumulated_error_bound": self.total_bound,
            "participating_tables": sorted(self.sources),
            "batch_interval": (
                [self.first_arrival, self.last_arrival]
                if self.first_arrival is not None and self.last_arrival is not None
                else None
            ),
            "steps": self.steps,
        }


def sum_with_bound(
    values: Iterable[float | int],
    operation: str,
    tables: Iterable[str],
    interval: tuple[int, int] | None,
    tracker: ErrorTracker,
) -> float:
    items = [float(value) for value in values]
    if not items:
        result = 0.0
        bound = 0.0
    else:
        result = math.fsum(items)
        count = len(items)
        gamma = count * FLOAT_EPS / (1.0 - count * FLOAT_EPS) if count * FLOAT_EPS < 0.5 else math.inf
        bound = gamma * math.fsum(abs(item) for item in items) + math.ulp(result)
    tracker.add(operation, result, bound, tables, interval, terms=len(items))
    return result


def product_with_bound(
    values: Iterable[float | int],
    operation: str,
    tables: Iterable[str],
    interval: tuple[int, int] | None,
    tracker: ErrorTracker,
) -> float:
    items = [float(value) for value in values]
    if not items:
        result = 1.0
        bound = 0.0
    else:
        result = 1.0
        bound = 0.0
        for item in items:
            previous = abs(result)
            result *= item
            bound = previous * math.ulp(item) + abs(item) * bound + math.ulp(result)
    tracker.add(operation, result, bound, tables, interval, factors=len(items))
    return result


def ratio_with_bound(
    numerator: float,
    numerator_bound: float,
    denominator: float,
    denominator_bound: float,
    operation: str,
    tables: Iterable[str],
    interval: tuple[int, int] | None,
    tracker: ErrorTracker,
    fallback: float = 0.0,
    **detail: Any,
) -> float:
    if denominator == 0.0:
        result = fallback
        bound = max(numerator_bound, denominator_bound)
    else:
        result = numerator / denominator
        absolute_denominator = abs(denominator)
        bound = (
            numerator_bound / absolute_denominator
            + abs(numerator) * denominator_bound / (absolute_denominator * absolute_denominator)
            + math.ulp(result)
        )
    tracker.add(operation, result, bound, tables, interval, **detail)
    return result


def relative_difference_with_bound(
    observed: float,
    expected: float,
    operation: str,
    tables: Iterable[str],
    interval: tuple[int, int] | None,
    tracker: ErrorTracker,
) -> tuple[float, float]:
    expected_bound = math.ulp(expected) if expected != 0.0 else 0.0
    observed_bound = math.ulp(observed) if observed != 0.0 else 0.0
    numerator = abs(observed - expected)
    numerator_bound = observed_bound + expected_bound + math.ulp(numerator)
    ratio = ratio_with_bound(
        numerator,
        numerator_bound,
        max(abs(expected), 1.0),
        expected_bound,
        operation,
        tables,
        interval,
        tracker,
        observed=observed,
        expected=expected,
    )
    return ratio, ratio * FLOAT_EPS + numerator_bound / max(abs(expected), 1.0)
