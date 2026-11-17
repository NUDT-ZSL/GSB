"""Deterministic health-state transitions with explicit reasons."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .models import InstanceRuntime, ProbeResult, StateTransition


@dataclass(frozen=True)
class StateChange:
    from_state: str
    to_state: str
    reason: str


def apply_probe_result(
    runtime: InstanceRuntime,
    result: ProbeResult,
) -> StateChange | None:
    """Update counters in ``runtime`` and return the transition, if any.

    Success/failure thresholds are consecutive.  A state only changes when the
    relevant threshold is met, so one transient probe cannot flip a healthy
    instance to unhealthy when the policy requires two failures.
    """

    threshold_ok = False
    if result.final_conclusion == "success":
        runtime.consecutive_successes = min(
            runtime.probe.success_threshold, runtime.consecutive_successes + 1
        )
        runtime.consecutive_failures = 0
        threshold_ok = runtime.consecutive_successes >= runtime.probe.success_threshold
        target = "healthy"
        reason = "success_threshold"
    else:
        runtime.consecutive_failures = min(
            runtime.probe.failure_threshold, runtime.consecutive_failures + 1
        )
        runtime.consecutive_successes = 0
        threshold_ok = runtime.consecutive_failures >= runtime.probe.failure_threshold
        target = "unhealthy"
        reason = "failure_threshold"

    if threshold_ok and runtime.health_state != target:
        previous = runtime.health_state
        runtime.health_state = target
        runtime.requires_reprobe = False
        return StateChange(previous, target, reason)
    return None


def mark_missing_probe_unknown(runtime: InstanceRuntime) -> StateChange | None:
    """Apply the no-evidence rule for an instance without a probe record."""

    if runtime.health_state == "unknown" and not runtime.requires_reprobe:
        return None
    previous = runtime.health_state
    runtime.health_state = "unknown"
    runtime.consecutive_successes = 0
    runtime.consecutive_failures = 0
    runtime.requires_reprobe = True
    return StateChange(previous, "unknown", "probe_missing")


def make_transition(
    *,
    change: StateChange,
    run_id: str,
    round_no: int,
    service_id: str,
    instance_id: str,
) -> StateTransition:
    return StateTransition(
        run_id=run_id,
        round_no=round_no,
        service_id=service_id,
        instance_id=instance_id,
        from_state=change.from_state,
        to_state=change.to_state,
        reason=change.reason,
        at_mono_ms=int(time.monotonic() * 1000),
        at_epoch_ms=int(time.time() * 1000),
    )
