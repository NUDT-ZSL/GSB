"""Rolling-window aggregation in full and fixed-memory streaming modes."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Iterable, Sequence

from .budget import compute_budget_metrics
from .config import EvaluationConfig
from .models import Event, WindowResult, minute_to_iso


MINUTE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class AggregationResult:
    long_windows: tuple[WindowResult, ...]
    short_windows: tuple[WindowResult, ...]
    path: str


def _make_window(
    *,
    kind: str,
    index: int,
    start_minute: int,
    length_minutes: int,
    step_minutes: int,
    last_minute: int,
    total_requests: int,
    failed_requests: int,
    observed_minutes: int,
    config: EvaluationConfig,
) -> WindowResult:
    end_minute = start_minute + length_minutes * MINUTE_SECONDS
    coverage_end = min(end_minute, last_minute + MINUTE_SECONDS)
    coverage_minutes = max(0, (coverage_end - start_minute) // MINUTE_SECONDS)
    partial = coverage_minutes < length_minutes
    precision = config.precision
    metrics = compute_budget_metrics(
        total_requests,
        failed_requests,
        config.slo.target_ppm,
        ratio_scale=precision.ratio_scale,
        burn_scale=precision.burn_scale,
        request_scale=precision.request_scale,
    )

    return WindowResult(
        window_id=f"{kind}-{index + 1:06d}",
        kind=kind,
        start_minute=start_minute,
        end_minute_exclusive=end_minute,
        start_iso=minute_to_iso(start_minute),
        end_iso_exclusive=minute_to_iso(end_minute),
        length_minutes=length_minutes,
        step_minutes=step_minutes,
        partial=partial,
        coverage_minutes=coverage_minutes,
        observed_minutes=observed_minutes,
        total_requests=total_requests,
        failed_requests=failed_requests,
        sli=metrics.sli,
        error_rate=metrics.error_rate,
        budget_fraction=metrics.budget_fraction,
        budget_total=metrics.budget_total,
        budget_consumed=metrics.budget_consumed,
        budget_remaining=metrics.budget_remaining,
        burn_rate=metrics.burn_rate,
        slo_version=config.slo.version,
        exact_sli=metrics.exact_sli,
        exact_burn_rate=metrics.exact_burn_rate,
    )


class FullWindowAggregator:
    """Prefix-sum aggregator for datasets below the adaptive threshold."""

    def __init__(self, config: EvaluationConfig) -> None:
        self.config = config

    def aggregate(self, events: Sequence[Event] | Iterable[Event]) -> AggregationResult:
        rows = events if isinstance(events, (list, tuple)) else tuple(events)
        if not rows:
            return AggregationResult((), (), "full")

        timestamps: list[int] = []
        prefix_total: list[int] = [0]
        prefix_failed: list[int] = [0]
        for event in rows:
            timestamps.append(event.minute_start)
            prefix_total.append(prefix_total[-1] + event.total_requests)
            prefix_failed.append(prefix_failed[-1] + event.failed_requests)

        anchor = timestamps[0]
        last_minute = timestamps[-1]
        step_seconds = self.config.windows.step_minutes * MINUTE_SECONDS
        window_count = (last_minute - anchor) // step_seconds + 1
        long_windows: list[WindowResult] = []
        short_windows: list[WindowResult] = []

        for index in range(window_count):
            start = anchor + index * step_seconds
            for kind, length, destination in (
                ("long", self.config.windows.long_minutes, long_windows),
                ("short", self.config.windows.short_minutes, short_windows),
            ):
                end = start + length * MINUTE_SECONDS
                left = bisect_left(timestamps, start)
                right = bisect_left(timestamps, end)
                total = prefix_total[right] - prefix_total[left]
                failed = prefix_failed[right] - prefix_failed[left]
                destination.append(
                    _make_window(
                        kind=kind,
                        index=index,
                        start_minute=start,
                        length_minutes=length,
                        step_minutes=self.config.windows.step_minutes,
                        last_minute=last_minute,
                        total_requests=total,
                        failed_requests=failed,
                        observed_minutes=right - left,
                        config=self.config,
                    )
                )
        return AggregationResult(tuple(long_windows), tuple(short_windows), "full")


class _EventRing:
    """Fixed-capacity ring of events with cumulative sums for interval queries."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("ring capacity must be positive")
        self.capacity = capacity
        self._timestamps = [0] * capacity
        self._cumulative_total = [0] * capacity
        self._cumulative_failed = [0] * capacity
        self._head = 0
        self._size = 0
        self._base_total = 0
        self._base_failed = 0
        self._last_total = 0
        self._last_failed = 0

    @property
    def size(self) -> int:
        return self._size

    def append(self, event: Event) -> None:
        if self._size >= self.capacity:
            raise RuntimeError("streaming ring capacity exceeded; this indicates an aggregation invariant bug")
        physical = (self._head + self._size) % self.capacity
        self._last_total += event.total_requests
        self._last_failed += event.failed_requests
        self._timestamps[physical] = event.minute_start
        self._cumulative_total[physical] = self._last_total
        self._cumulative_failed[physical] = self._last_failed
        self._size += 1

    def evict_before(self, minute_start: int) -> None:
        while self._size > 0 and self._timestamps[self._head] < minute_start:
            self._base_total = self._cumulative_total[self._head]
            self._base_failed = self._cumulative_failed[self._head]
            self._head = (self._head + 1) % self.capacity
            self._size -= 1

    def _lower_bound(self, minute_start: int) -> int:
        low = 0
        high = self._size
        while low < high:
            middle = (low + high) // 2
            physical = (self._head + middle) % self.capacity
            if self._timestamps[physical] < minute_start:
                low = middle + 1
            else:
                high = middle
        return low

    def _prefix(self, count: int) -> tuple[int, int]:
        if count == 0:
            return self._base_total, self._base_failed
        physical = (self._head + count - 1) % self.capacity
        return self._cumulative_total[physical], self._cumulative_failed[physical]

    def query(self, start_minute: int, end_minute: int) -> tuple[int, int, int]:
        left = self._lower_bound(start_minute)
        right = self._lower_bound(end_minute)
        left_total, left_failed = self._prefix(left)
        right_total, right_failed = self._prefix(right)
        return right_total - left_total, right_failed - left_failed, right - left


class _OnlineWindowRoute:
    def __init__(self, kind: str, length_minutes: int, config: EvaluationConfig, anchor: int) -> None:
        self.kind = kind
        self.length_minutes = length_minutes
        self.config = config
        self.anchor = anchor
        self.next_start = anchor
        self.next_index = 0
        self.windows: list[WindowResult] = []

    def advance_before(self, event_minute: int, ring: _EventRing) -> None:
        length_seconds = self.length_minutes * MINUTE_SECONDS
        step_seconds = self.config.windows.step_minutes * MINUTE_SECONDS
        while self.next_start + length_seconds <= event_minute:
            self._emit(ring, last_minute=event_minute)
            self.next_start += step_seconds
            self.next_index += 1

    def finalize(self, last_minute: int, ring: _EventRing) -> None:
        step_seconds = self.config.windows.step_minutes * MINUTE_SECONDS
        while self.next_start <= last_minute:
            self._emit(ring, last_minute=last_minute)
            self.next_start += step_seconds
            self.next_index += 1

    def _emit(self, ring: _EventRing, *, last_minute: int) -> None:
        end = self.next_start + self.length_minutes * MINUTE_SECONDS
        total, failed, observed = ring.query(self.next_start, end)
        self.windows.append(
            _make_window(
                kind=self.kind,
                index=self.next_index,
                start_minute=self.next_start,
                length_minutes=self.length_minutes,
                step_minutes=self.config.windows.step_minutes,
                last_minute=last_minute,
                total_requests=total,
                failed_requests=failed,
                observed_minutes=observed,
                config=self.config,
            )
        )


class StreamingWindowAggregator:
    """Single-pass aggregation with a fixed event ring.

    Input events are consumed in increasing timestamp order. Only events that can
    still belong to an unfinalized long or short window are retained. Window
    metadata is output state and necessarily grows with the number of windows.
    """

    def __init__(self, config: EvaluationConfig) -> None:
        self.config = config
        self._ring = _EventRing(max(config.windows.long_minutes, config.windows.short_minutes) + 2)
        self._long_route: _OnlineWindowRoute | None = None
        self._short_route: _OnlineWindowRoute | None = None
        self._first_minute: int | None = None
        self._last_minute: int | None = None
        self._finalized = False

    def feed(self, event: Event) -> None:
        if self._finalized:
            raise RuntimeError("cannot feed a finalized streaming aggregator")
        if self._first_minute is None:
            self._first_minute = event.minute_start
            self._long_route = _OnlineWindowRoute(
                "long", self.config.windows.long_minutes, self.config, event.minute_start
            )
            self._short_route = _OnlineWindowRoute(
                "short", self.config.windows.short_minutes, self.config, event.minute_start
            )
        assert self._long_route is not None and self._short_route is not None
        self._long_route.advance_before(event.minute_start, self._ring)
        self._short_route.advance_before(event.minute_start, self._ring)
        self._ring.evict_before(min(self._long_route.next_start, self._short_route.next_start))
        self._ring.append(event)
        self._last_minute = event.minute_start

    def finalize(self) -> AggregationResult:
        if self._finalized:
            raise RuntimeError("streaming aggregator was already finalized")
        self._finalized = True
        if self._last_minute is None:
            return AggregationResult((), (), "streaming")
        assert self._long_route is not None and self._short_route is not None
        self._long_route.finalize(self._last_minute, self._ring)
        self._short_route.finalize(self._last_minute, self._ring)
        if len(self._long_route.windows) != len(self._short_route.windows):
            raise RuntimeError("long and short streaming routes produced different window counts")
        return AggregationResult(
            tuple(self._long_route.windows), tuple(self._short_route.windows), "streaming"
        )


def aggregate_events(events: Sequence[Event] | Iterable[Event], config: EvaluationConfig) -> AggregationResult:
    return FullWindowAggregator(config).aggregate(events)
