from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .models import DiagnosticConfig, ProbeRecord

_OBSERVED_OUTCOMES = {
    "success",
    "status_mismatch",
    "timeout",
    "connection_refused",
    "malformed_response",
}
_FAILURE_OUTCOMES = _OBSERVED_OUTCOMES - {"success"}


def percentile(values: Sequence[int | float], p: float) -> float | None:
    """Linear-interpolation percentile over a definite sorted order."""

    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = position - lower
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return round(value, 3)


@dataclass(frozen=True)
class WindowSummary:
    target: str
    config_version: int
    window_index: int
    start_position: int
    end_position: int
    sample_count: int
    observed_count: int
    success_count: int
    failure_count: int
    abandoned_count: int
    not_started_abandoned_count: int
    active_abandoned_count: int
    loss_rate: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    jitter_ms: float | None
    start_plan_index: int
    end_plan_index: int
    start_seq: int
    end_seq: int
    outcome_counts: dict[str, int]
    excluded_abandoned: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "config_version": self.config_version,
            "window_index": self.window_index,
            "start_position": self.start_position,
            "end_position": self.end_position,
            "sample_count": self.sample_count,
            "observed_count": self.observed_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "abandoned_count": self.abandoned_count,
            "not_started_abandoned_count": self.not_started_abandoned_count,
            "active_abandoned_count": self.active_abandoned_count,
            "loss_rate": self.loss_rate,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "jitter_ms": self.jitter_ms,
            "start_plan_index": self.start_plan_index,
            "end_plan_index": self.end_plan_index,
            "start_seq": self.start_seq,
            "end_seq": self.end_seq,
            "outcome_counts": self.outcome_counts,
            "excluded_abandoned": list(self.excluded_abandoned),
            "denominator": self.observed_count,
            "exclusion_rule": (
                "abandoned probes are excluded from the loss denominator; "
                "observed_count counts only success/status_mismatch/timeout/"
                "connection_refused/malformed_response"
            ),
            "percentile_rule": (
                "sorted ascending; linear interpolation at (n-1)*p; "
                "latency uses successful observations only"
            ),
        }


def compute_windows(
    records: Iterable[ProbeRecord],
    config: DiagnosticConfig,
    config_version: int,
    target: str,
) -> tuple[WindowSummary, ...]:
    ordered = sorted(
        (record for record in records if record.target == target),
        key=lambda item: item.plan_index,
    )
    if not ordered:
        return tuple()
    windows: list[WindowSummary] = []
    index = 0
    start = 0
    total = len(ordered)
    while start < total:
        end = min(start + config.window_size, total)
        window_records = ordered[start:end]
        windows.append(
            _summarize_window(
                target=target,
                config_version=config_version,
                index=index,
                start=start,
                end=end,
                records=window_records,
            )
        )
        index += 1
        if end >= total:
            break
        start += config.window_step
    return tuple(windows)


def _summarize_window(
    *,
    target: str,
    config_version: int,
    index: int,
    start: int,
    end: int,
    records: Sequence[ProbeRecord],
) -> WindowSummary:
    outcome_counts = {
        outcome: sum(1 for record in records if record.outcome == outcome)
        for outcome in sorted(_OBSERVED_OUTCOMES | {"abandoned"})
    }
    observed = [record for record in records if record.outcome in _OBSERVED_OUTCOMES]
    successful = [record for record in observed if record.outcome == "success"]
    failures = [record for record in observed if record.outcome in _FAILURE_OUTCOMES]
    abandoned = [record for record in records if record.outcome == "abandoned"]
    latency_values = [
        record.observed_latency_ms
        for record in successful
        if record.observed_latency_ms is not None
    ]
    p50 = percentile(latency_values, 0.50)
    p95 = percentile(latency_values, 0.95)
    p99 = percentile(latency_values, 0.99)
    jitter = (
        round(p95 - p50, 3)
        if p50 is not None and p95 is not None
        else None
    )
    loss_rate = (
        round(len(failures) / len(observed), 6) if observed else None
    )
    excluded = tuple(
        {
            "seq": record.seq,
            "plan_index": record.plan_index,
            "abandon_kind": record.abandon_kind,
            "reason": record.detail,
            "excluded_from_denominator": True,
        }
        for record in sorted(abandoned, key=lambda item: item.plan_index)
    )
    return WindowSummary(
        target=target,
        config_version=config_version,
        window_index=index,
        start_position=start,
        end_position=end - 1,
        sample_count=len(records),
        observed_count=len(observed),
        success_count=len(successful),
        failure_count=len(failures),
        abandoned_count=len(abandoned),
        not_started_abandoned_count=sum(
            1 for record in abandoned if record.abandon_kind == "not_started_budget"
        ),
        active_abandoned_count=sum(
            1 for record in abandoned if record.abandon_kind == "active_budget_cutoff"
        ),
        loss_rate=loss_rate,
        p50_ms=p50,
        p95_ms=p95,
        p99_ms=p99,
        jitter_ms=jitter,
        start_plan_index=records[0].plan_index,
        end_plan_index=records[-1].plan_index,
        start_seq=records[0].seq,
        end_seq=records[-1].seq,
        outcome_counts=outcome_counts,
        excluded_abandoned=excluded,
    )


def recompute_target_windows(
    records: Iterable[ProbeRecord],
    config: DiagnosticConfig,
    config_version: int,
) -> dict[str, tuple[WindowSummary, ...]]:
    target_names = sorted({record.target for record in records})
    return {
        target: compute_windows(records, config, config_version, target)
        for target in target_names
    }
