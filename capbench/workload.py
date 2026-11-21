from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Sequence

from .clock import ClockSpec, InjectedLogicalClock
from .scenario import ERROR_CATEGORIES, GradientSpec


@dataclass(frozen=True)
class PlannedRequest:
    sequence: int
    worker: int
    planned_ms: int
    started_ms: int


@dataclass(frozen=True)
class RequestOutcome:
    status: int | None = None
    latency_ms: float | None = None
    category: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class WindowExecution:
    summary: dict[str, Any]
    outcomes: tuple[RequestOutcome, ...]
    requests: tuple[PlannedRequest, ...]


def _window_bounds(gradient: GradientSpec, window_index: int) -> tuple[int, int]:
    if window_index < 0 or window_index >= gradient.window_count:
        raise IndexError("window index outside gradient")
    local_start = window_index * gradient.window_ms
    local_end = min(gradient.duration_ms, local_start + gradient.window_ms)
    return local_start, local_end


def _request_count_between(
    gradient: GradientSpec,
    window_start_ms: int,
    window_end_ms: int,
) -> int:
    count = 0
    for worker in range(gradient.concurrency):
        offset = (worker * gradient.pace_ms) // gradient.concurrency
        first_k = max(
            0,
            math.ceil((window_start_ms - offset) / gradient.pace_ms),
        )
        last_k = math.floor(
            (window_end_ms - 1 - offset) / gradient.pace_ms
        )
        if last_k >= first_k:
            count += last_k - first_k + 1
    return count


def count_window_requests(gradient: GradientSpec, window_index: int) -> int:
    start_ms, end_ms = _window_bounds(gradient, window_index)
    return _request_count_between(gradient, start_ms, end_ms)


def planned_window_count(gradient: GradientSpec) -> int:
    return sum(
        count_window_requests(gradient, window_index)
        for window_index in range(gradient.window_count)
    )


def build_window_requests(
    gradient: GradientSpec,
    window_index: int,
    clock_spec: ClockSpec,
    request_seq_start: int,
) -> list[PlannedRequest]:
    """Build a request stream using only the injected logical clock.

    ``pace_ms`` is a per-virtual-user request interval.  User start offsets are
    spread over one pacing period.  The clock step table determines the actual
    tick at which each due request is emitted; no host-clock API participates.
    """

    if request_seq_start <= 0:
        raise ValueError("request_seq_start must be positive")
    window_start, window_end = _window_bounds(gradient, window_index)
    events: list[tuple[int, int]] = []
    for worker in range(gradient.concurrency):
        offset = (worker * gradient.pace_ms) // gradient.concurrency
        planned_ms = offset
        while planned_ms < gradient.duration_ms:
            if window_start <= planned_ms < window_end:
                events.append((planned_ms, worker))
            planned_ms += gradient.pace_ms
    events.sort(key=lambda item: (item[0], item[1]))

    cursor = InjectedLogicalClock(clock_spec, start_ms=window_start)
    ticks = list(cursor.ticks_until(window_end))
    if not ticks:
        return []

    requests: list[PlannedRequest] = []
    tick_index = 0
    for offset, (planned_ms, worker) in enumerate(events):
        while (
            tick_index + 1 < len(ticks)
            and ticks[tick_index] < planned_ms
        ):
            tick_index += 1
        started_ms = ticks[tick_index]
        if started_ms < planned_ms:
            started_ms = ticks[-1]
        requests.append(
            PlannedRequest(
                sequence=request_seq_start + offset,
                worker=worker,
                planned_ms=gradient.logical_start_ms + planned_ms,
                started_ms=gradient.logical_start_ms + started_ms,
            )
        )
    return requests


def p95_latency(values: Sequence[float]) -> float | None:
    """P95 with sorted linear interpolation at rank ``0.95 * (n - 1)``."""

    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = 0.95 * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        result = ordered[lower]
    else:
        fraction = position - lower
        result = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return round(result, 6)


def _merge_sequences(sequences: list[int]) -> list[list[int]]:
    if not sequences:
        return []
    ordered = sorted(sequences)
    ranges: list[list[int]] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append([start, previous])
        start = previous = value
    ranges.append([start, previous])
    return ranges


def _schedule_digest(requests: Sequence[PlannedRequest]) -> str:
    canonical = json.dumps(
        [[request.sequence, request.worker, request.planned_ms, request.started_ms] for request in requests],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def summarize_window(
    *,
    tenant: str,
    gradient: GradientSpec,
    window_index: int,
    requests: Sequence[PlannedRequest],
    outcomes: Sequence[RequestOutcome],
    expected_status: int,
) -> dict[str, Any]:
    if len(requests) != len(outcomes):
        raise ValueError("request and outcome counts differ")
    window_start, window_end = _window_bounds(gradient, window_index)
    error_counts = {category: 0 for category in ERROR_CATEGORIES}
    error_sequences: dict[str, list[int]] = {
        category: [] for category in ERROR_CATEGORIES
    }
    error_details: dict[str, list[dict[str, Any]]] = {
        category: [] for category in ERROR_CATEGORIES
    }
    status_codes: dict[str, int] = {}
    latencies: list[float] = []
    success_count = 0

    for request, outcome in zip(requests, outcomes, strict=True):
        if outcome.category is None:
            success_count += 1
        else:
            if outcome.category not in error_counts:
                raise ValueError(f"unknown error category: {outcome.category}")
            error_counts[outcome.category] += 1
            error_sequences[outcome.category].append(request.sequence)
            if len(error_details[outcome.category]) < 3:
                error_details[outcome.category].append(
                    {
                        "request_seq": request.sequence,
                        "detail": outcome.detail or outcome.category,
                    }
                )
        if outcome.status is not None:
            key = str(outcome.status)
            status_codes[key] = status_codes.get(key, 0) + 1
        if outcome.latency_ms is not None:
            latencies.append(float(outcome.latency_ms))

    request_count = len(requests)
    success_rate = success_count / request_count if request_count else 0.0
    p95 = p95_latency(latencies)
    anomalies = []
    for category in ERROR_CATEGORIES:
        if error_counts[category] == 0:
            continue
        anomalies.append(
            {
                "tenant": tenant,
                "gradient_index": gradient.index,
                "window_index": window_index,
                "category": category,
                "count": error_counts[category],
                "request_seq_ranges": _merge_sequences(error_sequences[category]),
                "samples": error_details[category],
            }
        )

    seq_values = [request.sequence for request in requests]
    return {
        "tenant": tenant,
        "gradient_index": gradient.index,
        "window_index": window_index,
        "window_start_ms": gradient.logical_start_ms + window_start,
        "window_end_ms": gradient.logical_start_ms + window_end,
        "expected_status": expected_status,
        "request_seq_start": min(seq_values) if seq_values else None,
        "request_seq_end": max(seq_values) if seq_values else None,
        "request_count": request_count,
        "success_count": success_count,
        "success_rate": round(success_rate, 6),
        "p95_latency_ms": p95,
        "latency_sample_count": len(latencies),
        "min_latency_ms": round(min(latencies), 6) if latencies else None,
        "max_latency_ms": round(max(latencies), 6) if latencies else None,
        "status_codes": dict(sorted(status_codes.items())),
        "error_counts": error_counts,
        "anomalies": anomalies,
        "schedule_sha256": _schedule_digest(requests),
        "p95_rule": "sorted_linear_interpolation_rank_0.95_n_minus_1",
    }

