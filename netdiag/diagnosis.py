from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .models import DiagnosticConfig, ProbeRecord, TargetSpec
from .stats import WindowSummary, compute_windows

_FAILURE_KINDS = (
    "timeout",
    "connection_refused",
    "malformed_response",
    "status_mismatch",
)
_DOMINANCE_ORDER = {
    kind: index for index, kind in enumerate(_FAILURE_KINDS)
}


@dataclass(frozen=True)
class Diagnosis:
    target: str
    config_version: int
    state: str
    fault_domain: str
    fault_type: str
    loss_rate: float | None
    abandoned_count: int
    window: WindowSummary | None
    window_summary: dict[str, Any]
    decision_chain: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "config_version": self.config_version,
            "state": self.state,
            "fault_domain": self.fault_domain,
            "fault_type": self.fault_type,
            "loss_rate": self.loss_rate,
            "abandoned_count": self.abandoned_count,
            "window": self.window.as_dict() if self.window else None,
            "decision_chain": self.decision_chain,
        }


def diagnose_target(
    *,
    target: TargetSpec,
    records: Iterable[ProbeRecord],
    config: DiagnosticConfig,
    config_version: int,
) -> Diagnosis:
    ordered = sorted(
        (record for record in records if record.target == target.name),
        key=lambda item: item.plan_index,
    )
    windows = compute_windows(ordered, config, config_version, target.name)
    latest = windows[-1] if windows else None
    consecutive_failures = _consecutive_failures(ordered)
    effective_loss_threshold = max(
        0.0, config.loss_rate_threshold - config.tolerance.loss_rate
    )
    effective_jitter_threshold = max(
        0.0, config.latency_jitter_threshold_ms - config.tolerance.latency_ms
    )
    loss_triggered = (
        latest is not None
        and latest.loss_rate is not None
        and latest.loss_rate >= effective_loss_threshold
    )
    jitter_triggered = (
        latest is not None
        and latest.jitter_ms is not None
        and latest.jitter_ms >= effective_jitter_threshold
    )
    streak_triggered = consecutive_failures >= target.failure_threshold
    all_failed = (
        latest is not None
        and latest.observed_count >= target.failure_threshold
        and latest.success_count == 0
        and latest.failure_count > 0
    )

    if latest is not None and latest.observed_count == 0 and latest.abandoned_count > 0:
        state = "suspicious"
        fault_domain = "scheduler_budget"
        fault_type = "budget_abandonment"
        reason = (
            "all window probes were excluded budget abandonments; network "
            "health cannot be asserted from an empty loss denominator"
        )
    elif latest is None:
        state = "suspicious"
        fault_domain = "scheduler_budget"
        fault_type = "no_observations"
        reason = "no committed probe observations are available"
    elif all_failed:
        state = "unreachable"
        fault_domain, fault_type = _fault_for_window(latest)
        reason = (
            "the latest window contains no successful observation and at least "
            "failure_threshold observed failures"
        )
    elif loss_triggered or streak_triggered:
        state = "suspected_loss"
        if jitter_triggered and not loss_triggered:
            fault_domain, fault_type = "network_path", "latency_jitter"
        else:
            fault_domain, fault_type = _fault_for_window(latest)
        reason = (
            "window loss rate or consecutive-failure threshold was reached"
        )
    elif (
        latest.failure_count > 0
        or jitter_triggered
        or latest.abandoned_count > 0
    ):
        state = "suspicious"
        if jitter_triggered:
            fault_domain, fault_type = "network_path", "latency_jitter"
        elif latest.abandoned_count > 0 and latest.failure_count == 0:
            fault_domain, fault_type = "scheduler_budget", "budget_abandonment"
        else:
            fault_domain, fault_type = _fault_for_window(latest)
        reason = (
            "observed failures, latency jitter, or excluded abandonments are "
            "below the loss threshold but are not healthy"
        )
    else:
        state = "healthy"
        fault_domain, fault_type = "none", "none"
        reason = "the latest window is fully successful within configured thresholds"

    first_trigger = _first_trigger(
        state=state,
        windows=windows,
        records=ordered,
        target=target,
        config=config,
        config_version=config_version,
        effective_loss_threshold=effective_loss_threshold,
        effective_jitter_threshold=effective_jitter_threshold,
    )
    latest_dict = latest.as_dict() if latest else None
    excluded_abandoned = (
        list(latest.excluded_abandoned) if latest is not None else []
    )
    decision_chain = {
        "target": target.name,
        "config_version": config_version,
        "state": state,
        "fault_domain": fault_domain,
        "fault_type": fault_type,
        "reason": reason,
        "hit_window": (
            {
                "window_index": latest.window_index,
                "start_position": latest.start_position,
                "end_position": latest.end_position,
                "start_seq": latest.start_seq,
                "end_seq": latest.end_seq,
                "start_plan_index": latest.start_plan_index,
                "end_plan_index": latest.end_plan_index,
            }
            if latest
            else None
        ),
        "sample_counts": (
            {
                "scheduled": latest.sample_count,
                "observed": latest.observed_count,
                "success": latest.success_count,
                "failure": latest.failure_count,
                "abandoned": latest.abandoned_count,
                "not_started_abandoned": latest.not_started_abandoned_count,
                "active_abandoned": latest.active_abandoned_count,
                "consecutive_failures": consecutive_failures,
            }
            if latest
            else {
                "scheduled": 0,
                "observed": 0,
                "success": 0,
                "failure": 0,
                "abandoned": 0,
                "not_started_abandoned": 0,
                "active_abandoned": 0,
                "consecutive_failures": 0,
            }
        ),
        "excluded_abandoned": excluded_abandoned,
        "loss_rate": latest.loss_rate if latest else None,
        "latency": (
            {
                "p50_ms": latest.p50_ms,
                "p95_ms": latest.p95_ms,
                "p99_ms": latest.p99_ms,
                "jitter_ms": latest.jitter_ms,
                "jitter_rule": "p95 - p50",
            }
            if latest
            else None
        ),
        "thresholds": {
            "loss_rate": config.loss_rate_threshold,
            "loss_rate_effective": effective_loss_threshold,
            "failure_threshold": target.failure_threshold,
            "latency_jitter_ms": config.latency_jitter_threshold_ms,
            "latency_jitter_effective_ms": effective_jitter_threshold,
            "window_size": config.window_size,
            "window_step": config.window_step,
        },
        "first_trigger": first_trigger,
        "exclusion_basis": (
            "Only observed outcomes enter the denominator. Both not-started "
            "and active budget abandonments are listed above and excluded."
        ),
    }
    return Diagnosis(
        target=target.name,
        config_version=config_version,
        state=state,
        fault_domain=fault_domain,
        fault_type=fault_type,
        loss_rate=latest.loss_rate if latest else None,
        abandoned_count=latest.abandoned_count if latest else 0,
        window=latest,
        window_summary=latest_dict or {},
        decision_chain=decision_chain,
    )


def _consecutive_failures(records: Sequence[ProbeRecord]) -> int:
    count = 0
    for record in reversed(records):
        if record.outcome == "abandoned":
            continue
        if record.outcome == "success":
            break
        count += 1
    return count


def _fault_for_window(window: WindowSummary) -> tuple[str, str]:
    counts = {
        kind: window.outcome_counts.get(kind, 0) for kind in _FAILURE_KINDS
    }
    if sum(counts.values()) == 0:
        if window.abandoned_count:
            return "scheduler_budget", "budget_abandonment"
        if (
            window.jitter_ms is not None
            and window.jitter_ms > 0
            and window.success_count > 0
        ):
            return "network_path", "latency_jitter"
        return "none", "none"
    max_count = max(counts.values())
    winners = [
        kind for kind in _FAILURE_KINDS if counts[kind] == max_count
    ]
    if len(winners) > 1 or sum(counts.values()) > max_count:
        return "network_path", "packet_loss"
    winner = winners[0]
    if winner == "status_mismatch":
        return "http_application", "http_status_mismatch"
    if winner == "malformed_response":
        return "protocol", "malformed_response"
    if winner == "connection_refused":
        return "transport", "connection_refused"
    return "network_path", "timeout"


def _first_trigger(
    *,
    state: str,
    windows: Sequence[WindowSummary],
    records: Sequence[ProbeRecord],
    target: TargetSpec,
    config: DiagnosticConfig,
    config_version: int,
    effective_loss_threshold: float,
    effective_jitter_threshold: float,
) -> dict[str, Any] | None:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    if state == "healthy":
        return None
    for window in windows:
        if (
            window.loss_rate is not None
            and window.loss_rate >= effective_loss_threshold
            and window.failure_count > 0
        ):
            candidates.append(
                (
                    window.window_index,
                    window.start_plan_index,
                    {
                        "rule": "window_loss_rate",
                        "window_index": window.window_index,
                        "window_end_seq": window.end_seq,
                        "sample_count": window.observed_count,
                        "value": window.loss_rate,
                        "effective_threshold": round(effective_loss_threshold, 6),
                        "configured_threshold": config.loss_rate_threshold,
                        "config_version": config_version,
                    },
                )
            )
        if (
            window.jitter_ms is not None
            and window.jitter_ms >= effective_jitter_threshold
        ):
            candidates.append(
                (
                    window.window_index,
                    window.start_plan_index,
                    {
                        "rule": "window_latency_jitter",
                        "window_index": window.window_index,
                        "window_end_seq": window.end_seq,
                        "sample_count": window.success_count,
                        "value": window.jitter_ms,
                        "effective_threshold": round(
                            effective_jitter_threshold, 3
                        ),
                        "configured_threshold": config.latency_jitter_threshold_ms,
                        "config_version": config_version,
                    },
                )
            )
    streak = 0
    for record in records:
        if record.outcome == "abandoned":
            continue
        if record.outcome == "success":
            streak = 0
            continue
        streak += 1
        if streak == target.failure_threshold:
            candidates.append(
                (
                    windows[-1].window_index if windows else 0,
                    record.plan_index,
                    {
                        "rule": "consecutive_failures",
                        "window_index": (
                            _window_index_for_plan(windows, record.plan_index)
                        ),
                        "window_end_seq": record.seq,
                        "sample_count": streak,
                        "value": streak,
                        "effective_threshold": target.failure_threshold,
                        "configured_threshold": target.failure_threshold,
                        "config_version": config_version,
                    },
                )
            )
            break
    for record in records:
        if record.outcome == "abandoned":
            candidates.append(
                (
                    _window_index_for_plan(windows, record.plan_index),
                    record.plan_index,
                    {
                        "rule": "budget_exclusion",
                        "window_index": _window_index_for_plan(
                            windows, record.plan_index
                        ),
                        "window_end_seq": record.seq,
                        "sample_count": 1,
                        "value": record.abandon_kind,
                        "effective_threshold": "excluded",
                        "configured_threshold": "excluded",
                        "config_version": config_version,
                    },
                )
            )
            break
    if not candidates:
        for record in records:
            if record.outcome != "success":
                candidates.append(
                    (
                        _window_index_for_plan(windows, record.plan_index),
                        record.plan_index,
                        {
                            "rule": "first_non_success",
                            "window_index": _window_index_for_plan(
                                windows, record.plan_index
                            ),
                            "window_end_seq": record.seq,
                            "sample_count": 1,
                            "value": record.outcome,
                            "effective_threshold": "observed",
                            "configured_threshold": "observed",
                            "config_version": config_version,
                        },
                    )
                )
                break
    if not candidates:
        return None
    chosen = sorted(candidates, key=lambda item: (item[0], item[1]))[0][2]
    return chosen


def _window_index_for_plan(
    windows: Sequence[WindowSummary], plan_index: int
) -> int:
    for window in windows:
        if window.start_plan_index <= plan_index <= window.end_plan_index:
            return window.window_index
    return windows[-1].window_index if windows else 0
