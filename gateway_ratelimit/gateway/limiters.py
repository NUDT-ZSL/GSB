"""Three independent rate-limit paths implemented with integer arithmetic only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import (
    LIMIT_PATHS,
    PATH_TOKEN_BUCKET,
    PATH_WINDOW_COUNTER,
    PATH_WINDOW_LOG,
    Route,
)

# Explicit truncation policy used anywhere an integer division is required:
# floor division (toward negative infinity) on non-negative values equals
# truncation toward zero; every divisor here is strictly positive.
TRUNCATION = "floor division // (truncate toward zero for non-negative values)"


@dataclass(frozen=True)
class PathDecision:
    path: str
    allow: bool
    remaining: int
    reason: str
    detail: str


class TokenBucket:
    """Continuous refill, burst capacity, integer millisecond accounting."""

    def __init__(self, rate_per_sec: int, burst: int) -> None:
        self.rate_per_sec = rate_per_sec
        self.burst = burst
        self.tokens = burst
        self.last_ms: int | None = None
        self.refilled_events = 0

    def refill(self, ts_ms: int) -> int:
        if self.last_ms is None:
            self.last_ms = ts_ms
            return 0
        elapsed = ts_ms - self.last_ms
        if elapsed <= 0:
            return 0
        # floor() on non-negative milliseconds; fractional tokens are dropped
        # (truncation toward zero) and the discarded remainder stays implicit.
        added = (elapsed * self.rate_per_sec) // 1000
        if added:
            before = self.tokens
            self.tokens = min(self.burst, self.tokens + added)
            if self.tokens != before:
                self.refilled_events += 1
            # Advance the anchor only when integer tokens were produced; the
            # floored sub-token remainder keeps accruing across later requests.
            self.last_ms = ts_ms
        return added

    def admit(self, ts_ms: int) -> PathDecision:
        added = self.refill(ts_ms)
        if self.tokens >= 1:
            self.tokens -= 1
            return PathDecision(
                PATH_TOKEN_BUCKET,
                True,
                self.tokens,
                "admitted",
                f"refill+{added} floor(elapsed*rate/1000), consume 1",
            )
        return PathDecision(
            PATH_TOKEN_BUCKET,
            False,
            0,
            "bucket_empty",
            f"refill+{added} still below 1 token; integer refill truncated",
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "path": PATH_TOKEN_BUCKET,
            "tokens": self.tokens,
            "last_ms": self.last_ms,
            "rate_per_sec": self.rate_per_sec,
            "burst": self.burst,
            "refilled_events": self.refilled_events,
            "truncation": TRUNCATION,
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.tokens = int(snapshot["tokens"])
        self.last_ms = None if snapshot["last_ms"] is None else int(snapshot["last_ms"])
        self.refilled_events = int(snapshot.get("refilled_events", 0))


class SlidingWindowLog:
    """Exact sliding window over a sorted log of accepted timestamps."""

    def __init__(self, window_ms: int, max_in_window: int) -> None:
        self.window_ms = window_ms
        self.max_in_window = max_in_window
        self.log: list[int] = []
        self.dropped_by_age = 0

    def admit(self, ts_ms: int) -> PathDecision:
        before = len(self.log)
        # Log retention span: keep ts where ts > ts_ms - window_ms; entries on
        # the boundary (equal age) slide out, so the window is (t-w, t].
        cutoff = ts_ms - self.window_ms
        retained = [entry for entry in self.log if entry > cutoff]
        self.dropped_by_age += before - len(retained)
        self.log = retained
        if len(self.log) < self.max_in_window:
            self.log.append(ts_ms)
            return PathDecision(
                PATH_WINDOW_LOG,
                True,
                self.max_in_window - len(self.log),
                "admitted",
                f"retained {len(retained)} after age-out at cutoff={cutoff}, append",
            )
        return PathDecision(
            PATH_WINDOW_LOG,
            False,
            0,
            "window_full",
            f"{len(retained)} active requests already at limit {self.max_in_window}",
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "path": PATH_WINDOW_LOG,
            "log": list(self.log),
            "window_ms": self.window_ms,
            "max_in_window": self.max_in_window,
            "dropped_by_age": self.dropped_by_age,
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.log = [int(value) for value in snapshot["log"]]
        self.dropped_by_age = int(snapshot.get("dropped_by_age", 0))


class SlidingWindowCounter:
    """Weighted sliding-window count on epoch-aligned integer sub-counters."""

    def __init__(self, window_ms: int, max_in_window: int) -> None:
        if window_ms <= 0:
            # Kept explicit so the E_LIMIT injection path reports a stable code.
            raise ValueError("window_ms must be > 0")
        self.window_ms = window_ms
        self.max_in_window = max_in_window
        self.buckets: dict[int, int] = {}
        self.last_ts: int | None = None

    def _prune(self, ts_ms: int) -> int:
        current_index = ts_ms // self.window_ms
        cutoff_index = current_index - 1
        before = len(self.buckets)
        self.buckets = {
            index: count for index, count in self.buckets.items() if index >= cutoff_index
        }
        return before - len(self.buckets)

    def admit(self, ts_ms: int) -> PathDecision:
        pruned = self._prune(ts_ms)
        current_index = ts_ms // self.window_ms
        previous_index = current_index - 1
        offset = ts_ms % self.window_ms
        previous_count = self.buckets.get(previous_index, 0)
        # weight = (window - offset)/window, computed without floats; floor()
        # discards the fractional half of a request (truncate toward zero).
        weighted_previous = (previous_count * (self.window_ms - offset)) // self.window_ms
        current_count = self.buckets.get(current_index, 0)
        estimated = current_count + weighted_previous
        detail = (
            f"aligned bucket={current_index} offset={offset} prev={previous_count} "
            f"weighted={weighted_previous} current={current_count} pruned={pruned}"
        )
        if estimated < self.max_in_window:
            self.buckets[current_index] = current_count + 1
            self.last_ts = ts_ms
            return PathDecision(
                PATH_WINDOW_COUNTER,
                True,
                self.max_in_window - (current_count + 1 + weighted_previous),
                "admitted",
                detail,
            )
        self.last_ts = ts_ms
        return PathDecision(
            PATH_WINDOW_COUNTER,
            False,
            0,
            "estimated_window_full",
            detail + "; weighted estimate reached limit",
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "path": PATH_WINDOW_COUNTER,
            "buckets": {str(index): count for index, count in sorted(self.buckets.items())},
            "window_ms": self.window_ms,
            "max_in_window": self.max_in_window,
            "last_ts": self.last_ts,
            "truncation": TRUNCATION,
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.buckets = {int(index): int(count) for index, count in snapshot["buckets"].items()}
        self.last_ts = None if snapshot.get("last_ts") is None else int(snapshot["last_ts"])


class LimiterSet:
    """Runs all three paths independently for one (route, client) pair."""

    def __init__(self, route: Route) -> None:
        self.route_name = route.name
        self.bucket = TokenBucket(route.rate_per_sec, route.burst)
        self.counter = SlidingWindowCounter(route.window_ms, route.max_in_window)
        self.log = SlidingWindowLog(route.window_ms, route.max_in_window)

    def decide(self, ts_ms: int) -> dict[str, PathDecision]:
        # Fixed ordering: never iterate over a dict in an order-dependent way.
        return {
            PATH_TOKEN_BUCKET: self.bucket.admit(ts_ms),
            PATH_WINDOW_COUNTER: self.counter.admit(ts_ms),
            PATH_WINDOW_LOG: self.log.admit(ts_ms),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            PATH_TOKEN_BUCKET: self.bucket.snapshot(),
            PATH_WINDOW_COUNTER: self.counter.snapshot(),
            PATH_WINDOW_LOG: self.log.snapshot(),
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        for path in LIMIT_PATHS:
            if path not in snapshot:
                raise KeyError(f"missing limiter snapshot for {path}")
        self.bucket.restore(snapshot[PATH_TOKEN_BUCKET])
        self.counter.restore(snapshot[PATH_WINDOW_COUNTER])
        self.log.restore(snapshot[PATH_WINDOW_LOG])
