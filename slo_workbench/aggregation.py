"""Pure, deterministic window aggregation logic.

The same functions drive both the online pre-computation path and the
cache verification (exact recomputation) path, which is what makes the
bit-for-bit equality guarantee possible.  No I/O and no wall clock here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .decimal_math import (
    HUNDRED,
    ONE,
    UNIT_ROUND,
    ZERO,
    Tracked,
    ordered_sum,
    quantile_type7,
)
from .errors import BusinessError, E_EMPTY_WINDOW
from .models import Event, SLO


def home_window_no(clock_ms: int, window_step_ms: int) -> int:
    """Return the unique window index an event belongs to.

    Windows are half-open stepping bins: window ``k`` owns events with
    ``k * step <= clock < (k + 1) * step``.  Window *lifetime* (how long late
    events are still accepted) is governed separately by ``window_length_ms``.
    """
    return clock_ms // window_step_ms


def window_bounds(window_no: int, slo: SLO) -> Tuple[int, int]:
    start_ms = window_no * slo.window_step_ms
    end_ms = start_ms + slo.window_step_ms
    return start_ms, end_ms


def window_accepts_until(window_no: int, slo: SLO) -> int:
    """Logical clock value at which the window stops accepting late events."""
    start_ms = window_no * slo.window_step_ms
    return start_ms + slo.window_length_ms


def is_late(event: Event, window_no: int, slo: SLO, current_clock: int) -> bool:
    """An event is late when its home window already closed at ``current_clock``."""
    return current_clock > window_accepts_until(window_no, slo)


@dataclass
class WindowInputs:
    """The exact set of events that belong to one window (deterministic order)."""

    events: List[Event]


def _ratio(numerator: Tracked, denominator: Tracked) -> Tracked:
    try:
        return numerator.div_by(denominator)
    except ZeroDivisionError as exc:
        raise ZeroDivisionError("zero denominator") from exc


def compute_window(slo: SLO, window_no: int, events: Sequence[Event]) -> Dict[str, Any]:
    """Compute the full reproducible report for one window.

    Events are processed in sequence order (already strictly increasing).
    Counts accumulate with tracked addition error; every ratio uses one fixed
    division; quantiles use R-7 linear interpolation on an ascending sort where
    ties retain event-sequence order.

    Raises :class:`BusinessError` with ``E_EMPTY_WINDOW`` when the window has
    no valid requests, so callers can persist a diagnostic instead of a ratio.
    """
    start_ms, end_ms = window_bounds(window_no, slo)

    valid_events = [event for event in events if event.valid]
    total_events = len(events)
    valid_count = len(valid_events)
    if valid_count == 0:
        raise BusinessError(
            E_EMPTY_WINDOW,
            "window contains no valid requests",
            fields={
                "slo_name": slo.name,
                "window_no": window_no,
                "event_count": total_events,
            },
        )

    success_count = 0
    failure_count = 0
    good_count = 0
    # Fixed order: walk events in sequence order, add Decimal(1) per category.
    success_sum = Tracked(ZERO)
    failure_sum = Tracked(ZERO)
    good_sum = Tracked(ZERO)
    valid_sum = Tracked(ZERO)
    one = ONE

    latency_values: List[Decimal] = []

    for event in valid_events:
        valid_sum = valid_sum.add_value(one)
        if event.success:
            success_count += 1
            success_sum = success_sum.add_value(one)
        else:
            failure_count += 1
            failure_sum = failure_sum.add_value(one)

        if slo.sli_type == "availability":
            event_good = event.success
        else:
            latency_values.append(event.latency_ms)
            event_good = event.latency_ms <= slo.latency_threshold_ms
        if event_good:
            good_count += 1
            good_sum = good_sum.add_value(one)

    # Ratios with a single fixed division each.
    good_ratio = _ratio(good_sum, valid_sum)
    error_ratio = Tracked(ONE, ZERO).subtract(good_ratio)

    target_ratio = Tracked(slo.target / HUNDRED, ZERO)
    budget_total_ratio = Tracked(ONE - target_ratio.value, target_ratio.error)

    budget_total = budget_total_ratio.multiply(valid_sum)
    budget_consumed = error_ratio.multiply(valid_sum)
    budget_remaining = budget_total.subtract(budget_consumed)

    allowed_error_ratio = budget_total_ratio
    if allowed_error_ratio.value == 0:
        # Target of 100% is rejected at validation, so this is defensive only.
        burn_rate = Tracked(ZERO, ZERO)
    else:
        burn_rate = _ratio(error_ratio, allowed_error_ratio)

    payload: Dict[str, Any] = {
        "slo_name": slo.name,
        "version": slo.version,
        "window_no": window_no,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "total_events": total_events,
        "valid_requests": valid_count,
        "success_count": success_count,
        "failure_count": failure_count,
        "good_count": good_count,
        "sli": {
            "type": slo.sli_type,
            "good_ratio": good_ratio.to_dict(),
            "error_ratio": error_ratio.to_dict(),
        },
        "budget": {
            "total": budget_total.to_dict(),
            "consumed": budget_consumed.to_dict(),
            "remaining": budget_remaining.to_dict(),
        },
        "burn_rate": burn_rate.to_dict(),
        "arithmetic": {
            "precision": 38,
            "rounding": "ROUND_HALF_EVEN",
            "unit_round": str(UNIT_ROUND),
        },
    }

    if slo.sli_type == "latency":
        ordered = sorted(latency_values, key=lambda value: (value,))
        quantile_value = quantile_type7(ordered, slo.latency_percentile)
        within = sum(1 for value in ordered if value <= slo.latency_threshold_ms)
        within_sum = ordered_sum([ONE for _ in range(within)])
        threshold_ratio = _ratio(within_sum, valid_sum)
        payload["latency"] = {
            "percentile": str(slo.latency_percentile),
            "threshold_ms": str(slo.latency_threshold_ms),
            "quantile_ms": str(quantile_value),
            "quantile_met": quantile_value <= slo.latency_threshold_ms,
            "within_threshold_ratio": threshold_ratio.to_dict(),
            "sample_size": len(ordered),
        }

    payload["burn_exceeded"] = burn_rate.value > slo.burn_alert_threshold
    return payload


def compliance_window_numbers(
    current_window_no: int, slo: SLO
) -> List[int]:
    """Window numbers inside the compliance period ending at ``current_window_no``."""
    windows_per_period = slo.compliance_period_ms // slo.window_step_ms
    earliest = max(0, current_window_no - windows_per_period + 1)
    return list(range(earliest, current_window_no + 1))


def aggregate_compliance(
    slo: SLO, window_payloads: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """Roll window-level budgets up into one compliance-period burn conclusion.

    Window payloads are summed in ascending window number with the same
    left-to-right tracked addition used per window.
    """
    ordered = sorted(window_payloads, key=lambda item: item["window_no"])
    total = Tracked(ZERO)
    consumed = Tracked(ZERO)
    good = Tracked(ZERO)
    valid = Tracked(ZERO)
    window_refs: List[int] = []
    burn_values: List[Decimal] = []

    for payload in ordered:
        window_refs.append(payload["window_no"])
        total = total.add_value(Decimal(payload["budget"]["total"]["value"]))
        consumed = consumed.add_value(Decimal(payload["budget"]["consumed"]["value"]))
        good = good.add_value(Decimal(payload["good_count"]))
        valid = valid.add_value(Decimal(payload["valid_requests"]))
        burn_values.append(Decimal(payload["burn_rate"]["value"]))

    if total.value == 0:
        cumulative_burn = Tracked(ZERO, ZERO)
    else:
        cumulative_burn = _ratio(consumed, total)

    remaining = total.subtract(consumed)
    return {
        "slo_name": slo.name,
        "version": slo.version,
        "window_numbers": window_refs,
        "compliance_period_ms": slo.compliance_period_ms,
        "budget_total": total.to_dict(),
        "budget_consumed": consumed.to_dict(),
        "budget_remaining": remaining.to_dict(),
        "cumulative_burn_rate": cumulative_burn.to_dict(),
        "window_burn_rates": [str(value) for value in burn_values],
        "good_requests": str(good.value),
        "valid_requests": str(valid.value),
    }


def adjudicate_alert(
    slo: SLO,
    window_no: int,
    window_exceeded: bool,
    previous: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """State machine driven only by persisted streak counters and this window.

    States: ``ok`` -> ``firing`` after ``burn_consecutive_windows`` exceeded
    windows; ``firing`` -> ``ok`` after ``release_consecutive_windows``
    non-exceeded windows.  All inputs are persisted, so the verdict is
    reproducible after restart without any in-memory residue.
    """
    if previous is None:
        state = "ok"
        fire_streak = 0
        release_streak = 0
        since_window: Optional[int] = None
    else:
        state = previous["state"]
        fire_streak = previous["fire_streak"]
        release_streak = previous["release_streak"]
        since_window = previous["since_window"]

    transition = "none"
    reason = "within_threshold"

    if window_exceeded:
        fire_streak += 1
        release_streak = 0
        reason = "burn_rate_exceeded"
        if state != "firing" and fire_streak >= slo.burn_consecutive_windows:
            state = "firing"
            since_window = window_no
            transition = "fire"
            reason = f"burn rate exceeded for {fire_streak} consecutive window(s)"
    else:
        release_streak += 1
        fire_streak = 0
        if state == "firing" and release_streak >= slo.release_consecutive_windows:
            state = "ok"
            transition = "release"
            since_window = None
            reason = f"burn rate within threshold for {release_streak} consecutive window(s)"

    return {
        "slo_name": slo.name,
        "version": slo.version,
        "state": state,
        "fire_streak": fire_streak,
        "release_streak": release_streak,
        "since_window": since_window,
        "updated_window": window_no,
        "transition": transition,
        "reason": reason,
        "window_exceeded": window_exceeded,
    }
