"""Deterministic metric window aggregation.

Window membership is a pure function of the injected logical timestamp:

    window_start = floor(timestamp_ms / window_ms) * window_ms
    window_end   = window_start + window_ms

The interval is left-closed and right-open.  Samples are processed in
``(timestamp_ms, sample_id)`` order and percentile interpolation is performed
with Decimal arithmetic, so results are stable across runs and platforms.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_FLOOR,
    Decimal,
    localcontext,
)
from typing import Any, Iterable, Mapping, Sequence

from .errors import (
    CLOCK_ROLLBACK,
    DUPLICATE_SAMPLE_ID,
    INVALID_SAMPLE,
    SCHEMA_ERROR,
    UNKNOWN_METRIC,
    ControlError,
)
from .models import MetricSource
from .util import DECIMAL_CONTEXT_PRECISION, dec, decimal_text, integer


@dataclass(frozen=True)
class Sample:
    sample_id: str
    metric: str
    timestamp_ms: int
    value: Decimal
    sequence: int

    @classmethod
    def from_dict(cls, raw: Any, ordinal: int = 0) -> "Sample":
        if not isinstance(raw, Mapping):
            raise ControlError(
                INVALID_SAMPLE,
                "sample must be an object",
                {"index": ordinal},
            )
        metric = raw.get("metric", raw.get("metric_name"))
        if not isinstance(metric, str) or not metric:
            raise ControlError(
                INVALID_SAMPLE,
                "sample.metric must be a non-empty string",
                {"index": ordinal},
            )
        timestamp = integer(
            raw.get("timestamp_ms", raw.get("at_ms")),
            f"samples[{ordinal}].timestamp_ms",
            nonnegative=True,
        )
        value = dec(raw.get("value"), f"samples[{ordinal}].value")
        if value < 0:
            raise ControlError(
                INVALID_SAMPLE,
                "sample value must be non-negative",
                {"index": ordinal, "metric": metric, "value": decimal_text(value)},
            )
        sample_id_raw = raw.get("sample_id", raw.get("id"))
        if sample_id_raw is None:
            sample_id = f"{metric}:{timestamp}:{ordinal:08d}"
        elif isinstance(sample_id_raw, str) and sample_id_raw:
            sample_id = sample_id_raw
        else:
            raise ControlError(
                INVALID_SAMPLE,
                "sample.sample_id must be a non-empty string",
                {"index": ordinal},
            )
        return cls(
            sample_id=sample_id,
            metric=metric,
            timestamp_ms=timestamp,
            value=value,
            sequence=ordinal,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "metric": self.metric,
            "timestamp_ms": self.timestamp_ms,
            "value": decimal_text(self.value),
        }


@dataclass(frozen=True)
class WindowAggregate:
    metric: str
    window_start_ms: int
    window_end_ms: int
    aggregation: str
    percentile: Decimal | None
    sample_count: int
    aggregate: Decimal
    sample_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "metric": self.metric,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "aggregation": self.aggregation,
            "sample_count": self.sample_count,
            "aggregate": decimal_text(self.aggregate),
            "sample_ids": list(self.sample_ids),
        }
        if self.percentile is not None:
            result["percentile"] = decimal_text(self.percentile)
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WindowAggregate":
        percentile_raw = raw.get("percentile")
        return cls(
            metric=str(raw["metric"]),
            window_start_ms=int(raw["window_start_ms"]),
            window_end_ms=int(raw["window_end_ms"]),
            aggregation=str(raw["aggregation"]),
            percentile=None if percentile_raw is None else dec(percentile_raw),
            sample_count=int(raw["sample_count"]),
            aggregate=dec(raw["aggregate"]),
            sample_ids=tuple(str(item) for item in raw.get("sample_ids", [])),
        )


def parse_samples(payload: Any) -> list[Sample]:
    if isinstance(payload, Mapping):
        raw_samples = payload.get("samples", payload.get("stream", []))
    else:
        raw_samples = payload
    if not isinstance(raw_samples, list):
        raise ControlError(SCHEMA_ERROR, "samples must be an array")
    samples: list[Sample] = []
    by_id: dict[str, Sample] = {}
    for index, raw in enumerate(raw_samples):
        sample = Sample.from_dict(raw, index)
        previous = by_id.get(sample.sample_id)
        if previous is not None:
            if (
                previous.metric == sample.metric
                and previous.timestamp_ms == sample.timestamp_ms
                and previous.value == sample.value
            ):
                # Exact replays are harmless and still occupy one window slot.
                continue
            raise ControlError(
                DUPLICATE_SAMPLE_ID,
                f"sample_id {sample.sample_id!r} has conflicting content",
                {
                    "sample_id": sample.sample_id,
                    "first": previous.to_dict(),
                    "duplicate": sample.to_dict(),
                },
            )
        by_id[sample.sample_id] = sample
        samples.append(sample)
    return samples


def validate_sample_references(
    samples: Iterable[Sample], metrics: Mapping[str, MetricSource]
) -> None:
    for sample in samples:
        if sample.metric not in metrics:
            raise ControlError(
                UNKNOWN_METRIC,
                f"sample {sample.sample_id!r} references unknown metric {sample.metric!r}",
                {"sample_id": sample.sample_id, "metric": sample.metric},
            )


def check_clock_rollback(
    samples: Sequence[Sample],
    *,
    last_clock_ms: int = 0,
    tolerance_ms: int = 0,
) -> int:
    """Return the maximum observed logical timestamp.

    A rollback larger than ``tolerance_ms`` is rejected.  Small rollbacks are
    accepted to represent out-of-order delivery, but processing remains sorted.
    """

    maximum = last_clock_ms
    # Arrival order is significant for rollback detection.  Aggregation may
    # later sort for deterministic bucketing, but a stream that moves backwards
    # by more than the tolerance must be rejected before that normalisation.
    for sample in samples:
        if sample.timestamp_ms < maximum - tolerance_ms:
            raise ControlError(
                CLOCK_ROLLBACK,
                "logical clock moved backwards beyond tolerance",
                {
                    "timestamp_ms": sample.timestamp_ms,
                    "previous_max_ms": maximum,
                    "tolerance_ms": tolerance_ms,
                    "sample_id": sample.sample_id,
                    "metric": sample.metric,
                },
            )
        maximum = max(maximum, sample.timestamp_ms)
    return maximum


def _window_start(timestamp_ms: int, window_ms: int) -> int:
    return (timestamp_ms // window_ms) * window_ms


def aggregate_windows(
    metrics: Mapping[str, MetricSource],
    samples: Iterable[Sample],
    *,
    clock_ms: int | None = None,
    last_clock_ms: int = 0,
    rollback_tolerance_ms: int = 0,
) -> list[WindowAggregate]:
    """Aggregate samples into closed windows.

    If ``clock_ms`` is supplied, only windows with ``window_end <= clock_ms``
    are emitted.  The open window remains uncommitted and can receive more
    samples without double counting.
    """

    materialised = list(samples)
    validate_sample_references(materialised, metrics)
    check_clock_rollback(
        materialised,
        last_clock_ms=last_clock_ms,
        tolerance_ms=rollback_tolerance_ms,
    )
    grouped: dict[tuple[str, int], list[Sample]] = {}
    for sample in materialised:
        metric = metrics[sample.metric]
        start = _window_start(sample.timestamp_ms, metric.window_ms)
        grouped.setdefault((sample.metric, start), []).append(sample)

    results: list[WindowAggregate] = []
    for metric_name, window_start in sorted(grouped):
        metric = metrics[metric_name]
        window_end = window_start + metric.window_ms
        if clock_ms is not None and window_end > clock_ms:
            continue
        bucket = sorted(grouped[(metric_name, window_start)], key=lambda s: (s.timestamp_ms, s.sample_id))
        values = [sample.value for sample in bucket]
        with localcontext() as ctx:
            ctx.prec = DECIMAL_CONTEXT_PRECISION
            if metric.aggregation == "average":
                aggregate = sum(values, Decimal(0)) / Decimal(len(values))
            elif metric.aggregation == "percentile":
                assert metric.percentile is not None
                ordered = sorted(
                    zip(values, (sample.sample_id for sample in bucket)),
                    key=lambda pair: (pair[0], pair[1]),
                )
                rank = Decimal(len(ordered) - 1) * metric.percentile / Decimal(100)
                lower_rank = int(rank.to_integral_value(rounding=ROUND_FLOOR))
                fraction = rank - Decimal(lower_rank)
                lower_value = ordered[lower_rank][0]
                upper_value = ordered[min(lower_rank + 1, len(ordered) - 1)][0]
                aggregate = lower_value + (upper_value - lower_value) * fraction
            else:  # validated earlier; defensive branch
                raise ControlError(
                    SCHEMA_ERROR,
                    f"unsupported aggregation {metric.aggregation!r}",
                    {"metric": metric_name},
                )
        results.append(
            WindowAggregate(
                metric=metric_name,
                window_start_ms=window_start,
                window_end_ms=window_end,
                aggregation=metric.aggregation,
                percentile=metric.percentile,
                sample_count=len(bucket),
                aggregate=aggregate,
                sample_ids=tuple(sample.sample_id for sample in bucket),
            )
        )
    return results


def latest_window_per_metric(
    windows: Iterable[WindowAggregate],
) -> dict[str, WindowAggregate]:
    latest: dict[str, WindowAggregate] = {}
    for window in windows:
        current = latest.get(window.metric)
        if current is None or (
            window.window_end_ms,
            window.window_start_ms,
        ) > (
            current.window_end_ms,
            current.window_start_ms,
        ):
            latest[window.metric] = window
    return latest
