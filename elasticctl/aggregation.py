from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Sequence

import numpy as np

from .errors import fail
from .fixed import SCALE, fixed_text, quantize_float, to_fixed
from .model import MetricSample, MetricSource


@dataclass(frozen=True)
class AggregateResult:
    metric_source_id: str
    aggregation: str
    window_start_exclusive_ms: int
    window_end_ms: int
    sample_count: int
    fixed_value: int
    fixed_text: str
    float_text: str
    target_fixed: int
    alert_threshold_fixed: int
    interpolation: str
    paths_agree: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "metricSourceId": self.metric_source_id,
            "aggregation": self.aggregation,
            "windowStartExclusiveMs": self.window_start_exclusive_ms,
            "windowEndMs": self.window_end_ms,
            "sampleCount": self.sample_count,
            "fixedValue": self.fixed_text,
            "floatValue": self.float_text,
            "targetValue": fixed_text(self.target_fixed),
            "alertThreshold": fixed_text(self.alert_threshold_fixed),
            "pathsAgree": self.paths_agree,
            "fixedPoint": {
                "scale": SCALE,
                "rounding": "ROUND_HALF_UP",
                "roundingDirection": "away_from_zero_on_tie",
            },
            "floatingPoint": {
                "type": "binary64",
                "library": "numpy",
                "canonicalization": "decimal(str(value)) quantized to 0.000001 ROUND_HALF_UP",
            },
            "interpolation": self.interpolation,
        }


def window_bounds(window_ms: int, logical_now_ms: int) -> tuple[int, int]:
    return logical_now_ms - window_ms, logical_now_ms


def _fixed_average(values: Sequence[int]) -> int:
    total = sum(values)
    count = len(values)
    quotient, remainder = divmod(total, count)
    if remainder * 2 >= count:
        quotient += 1
    return quotient


def _fixed_percentile(values: Sequence[int], percentile: Decimal) -> int:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = Decimal(len(ordered) - 1) * percentile
    lower_index = int(rank.to_integral_value(rounding=ROUND_FLOOR))
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = rank - Decimal(lower_index)
    interpolated = Decimal(ordered[lower_index]) + fraction * Decimal(
        ordered[upper_index] - ordered[lower_index]
    )
    return int(interpolated.to_integral_value(rounding=ROUND_HALF_UP))


def _float_percentile(values: Sequence[float], percentile: Decimal) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = float(len(ordered) - 1) * float(percentile)
    lower_index = math.floor(rank)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = rank - lower_index
    return ordered[lower_index] + fraction * (
        ordered[upper_index] - ordered[lower_index]
    )


def aggregate_metric(
    metric: MetricSource,
    samples: Sequence[MetricSample],
    logical_now_ms: int,
) -> AggregateResult:
    start_exclusive, end = window_bounds(metric.window_ms, logical_now_ms)
    in_window = [
        sample
        for sample in samples
        if sample.metric_source_id == metric.id
        and start_exclusive < sample.timestamp_ms <= end
    ]
    in_window.sort(key=lambda sample: (sample.timestamp_ms, sample.value_fixed))
    if not in_window:
        fail(
            "E_EMPTY_WINDOW",
            "no metric samples exist in the requested logical window",
            {
                "metricSourceId": metric.id,
                "windowStartExclusiveMs": start_exclusive,
                "windowEndMs": end,
            },
        )

    fixed_values = [sample.value_fixed for sample in in_window]
    raw_float_values = [float(sample.value) for sample in in_window]
    if metric.aggregation == "avg":
        fixed_value = _fixed_average(fixed_values)
        float_raw = float(
            np.mean(np.asarray(raw_float_values, dtype=np.float64), dtype=np.float64)
        )
        interpolation = "mean; no interpolation"
    else:
        if metric.percentile is None:
            fail(
                "E_INVALID_AGGREGATION",
                "percentile aggregation is missing its percentile",
                {"metricSourceId": metric.id},
            )
        fixed_value = _fixed_percentile(fixed_values, metric.percentile)
        float_raw = _float_percentile(raw_float_values, metric.percentile)
        interpolation = (
            "sorted ascending; rank=(n-1)*p; linear interpolation; "
            "round half up to scale 0.000001"
        )

    fixed_canonical = fixed_text(fixed_value)
    float_canonical = format(quantize_float(float_raw), "f")
    paths_agree = fixed_canonical == float_canonical

    try:
        target_fixed = to_fixed(metric.target_value, "targetValue")
        alert_fixed = to_fixed(metric.alert_threshold, "alertThreshold")
    except (ArithmeticError, ValueError):
        fail(
            "E_INVALID_AGGREGATION",
            "metric thresholds could not be converted to fixed point",
            {"metricSourceId": metric.id},
        )

    return AggregateResult(
        metric_source_id=metric.id,
        aggregation=metric.aggregation,
        window_start_exclusive_ms=start_exclusive,
        window_end_ms=end,
        sample_count=len(in_window),
        fixed_value=fixed_value,
        fixed_text=fixed_canonical,
        float_text=float_canonical,
        target_fixed=target_fixed,
        alert_threshold_fixed=alert_fixed,
        interpolation=interpolation,
        paths_agree=paths_agree,
    )

