from __future__ import annotations

import math
from typing import Any


"""Independent reference adjudicator.

This module intentionally imports no code from ``engine``, ``normalizer``, or
``common``. It consumes only the shared normalized project JSON and rebuilds
the decision state machine independently.
"""


METRICS = ("error_rate", "latency_p95_ms", "session_drop_rate")


def _mean_value(pairs: list[tuple[float, int]]) -> float:
    numerator = 0.0
    denominator = 0
    for value, weight in pairs:
        numerator += value * weight
        denominator += weight
    return numerator / denominator


def _p95_value(pairs: list[tuple[float, int]]) -> float:
    ordered = sorted(pairs, key=lambda pair: pair[0])
    total = sum(weight for _, weight in ordered)
    target = max(1, int(math.ceil(0.95 * total)))
    running = 0
    for value, weight in ordered:
        running += weight
        if running >= target:
            return value
    return ordered[-1][0]


def _rounded(metric: str, value: float) -> float:
    if metric == "latency_p95_ms":
        return round(value, 3)
    return round(value, 9)


def _metric_window(
    step: dict[str, Any],
    step_start: int,
    expected: list[int],
    by_time: dict[int, dict[str, Any]],
    now: int,
    metric: str,
) -> dict[str, Any]:
    lower = max(
        step_start,
        now - int(step["sliding_window_seconds"]),
        now - int(step["observation_window_seconds"]),
    )
    expected_in_window = [time for time in expected if lower < time <= now]
    pairs: list[tuple[float, int]] = []
    used: list[int] = []
    for time in expected_in_window:
        sample = by_time.get(time)
        if sample is None or sample.get(metric) is None:
            continue
        pairs.append((float(sample[metric]), int(sample["sample_count"])))
        used.append(time)

    expected_count = len(expected_in_window)
    present_count = len(pairs)
    missing_count = expected_count - present_count
    missing_ratio = missing_count / expected_count if expected_count else 0.0
    degraded = missing_ratio > float(step["max_missing_ratio"])

    if degraded:
        value = None
        sample_count = 0
        used = []
    else:
        raw_value = _p95_value(pairs) if metric == "latency_p95_ms" else _mean_value(pairs)
        value = _rounded(metric, raw_value)
        sample_count = sum(weight for _, weight in pairs)

    return {
        "metric": metric,
        "unit": "milliseconds" if metric == "latency_p95_ms" else "fraction",
        "window": {"start_exclusive": lower, "end_inclusive": now},
        "expected_sample_points": expected_count,
        "present_sample_points": present_count,
        "missing_sample_points": missing_count,
        "missing_ratio": round(missing_ratio, 9),
        "sample_count": sample_count,
        "used_sample_times": used,
        "value": value,
        "missing_degraded": degraded,
        "threshold": step["thresholds"][metric],
        "operator": ">",
    }


def _basis(now: int, points: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "logical_time_seconds": now,
        "metrics": {
            metric: {
                "window": points[metric]["window"].copy(),
                "sample_count": points[metric]["sample_count"],
                "value": points[metric]["value"],
                "threshold": points[metric]["threshold"],
                "breached": points[metric]["breached"],
                "consecutive_breaches": points[metric][
                    "consecutive_breaches"
                ],
                "missing_ratio": points[metric]["missing_ratio"],
            }
            for metric in METRICS
        },
    }


def _close_runs(streak: dict[str, int], spikes: dict[str, int], keep: str | None = None):
    for metric in METRICS:
        if metric == keep:
            continue
        if streak[metric]:
            spikes[metric] += 1
            streak[metric] = 0


def _target(decision: dict[str, Any] | None) -> dict[str, Any]:
    if decision is None:
        return {
            "step_number": 0,
            "step_id": "baseline",
            "traffic_percent": "0",
            "traffic_ppm": 0,
        }
    return {
        "step_number": decision["step_number"],
        "step_id": decision["step_id"],
        "traffic_percent": decision["declared_traffic_percent"],
        "traffic_ppm": decision["declared_traffic_ppm"],
    }


def _blank_decision(step: dict[str, Any], reason: str) -> dict[str, Any]:
    decision_id = f"step-{step['step_number']}"
    return {
        "decision_id": decision_id,
        "ledger_ref": decision_id,
        "step_number": step["step_number"],
        "step_id": step["step_id"],
        "declared_traffic_percent": step["traffic_percent"],
        "declared_traffic_ppm": step["traffic_ppm"],
        "verdict": "not_evaluated",
        "reason": reason,
        "trigger": None,
        "pause_evidence": [],
        "resume_evidence": [],
        "rollback_target": None,
        "metric_basis": None,
        "reference_ledger": {
            "elapsed_window_seconds": 0,
            "paused_seconds": 0,
            "pause_count": 0,
            "rollback_count": 0,
            "metrics": {
                metric: {
                    "breach_count": 0,
                    "spike_count": 0,
                    "sustained_breach_count": 0,
                }
                for metric in METRICS
            },
        },
    }


def run_reference(project: dict[str, Any]) -> dict[str, Any]:
    """Adjudicate a normalized project without using the main engine."""
    steps = project["steps"]
    by_time = {
        int(sample["logical_time_seconds"]): sample
        for sample in project["observations"]
    }
    starts: list[int] = []
    cursor = 0
    for step in steps:
        starts.append(cursor)
        cursor += int(step["observation_window_seconds"])

    decisions: list[dict[str, Any]] = []
    stable: list[dict[str, Any]] = []
    rollback_target: dict[str, Any] | None = None
    stop_reason: str | None = None

    for step_index, step in enumerate(steps):
        if stop_reason:
            decisions.append(_blank_decision(step, stop_reason))
            continue

        start = starts[step_index]
        interval = int(step["sample_interval_seconds"])
        expected = list(
            range(
                start + interval,
                start + int(step["observation_window_seconds"]) + 1,
                interval,
            )
        )
        streak = {metric: 0 for metric in METRICS}
        breaches = {metric: 0 for metric in METRICS}
        spikes = {metric: 0 for metric in METRICS}
        sustained_counts = {metric: 0 for metric in METRICS}
        pause_evidence: list[dict[str, Any]] = []
        resume_evidence: list[dict[str, Any]] = []
        paused = False
        pause_episodes = 0
        executed_ticks = 0
        paused_ticks = 0
        trigger: dict[str, Any] | None = None
        final_basis: dict[str, Any] | None = None
        verdict = "promote"
        reason = "observation_window_completed_without_sustained_breach"

        for now in expected:
            executed_ticks += 1
            points: dict[str, dict[str, Any]] = {}
            for metric in METRICS:
                points[metric] = _metric_window(
                    step, start, expected, by_time, now, metric
                )
            missing = [
                metric
                for metric in METRICS
                if points[metric]["missing_degraded"]
            ]

            if missing:
                _close_runs(streak, spikes)
                newly_paused = not paused
                if newly_paused:
                    paused = True
                    pause_episodes += 1
                    evidence = {
                        "kind": "probe_missing",
                        "logical_time_seconds": now,
                        "window": points[METRICS[0]]["window"].copy(),
                        "metrics": [
                            {
                                "metric": metric,
                                "expected_points": points[metric][
                                    "expected_sample_points"
                                ],
                                "present_points": points[metric][
                                    "present_sample_points"
                                ],
                                "missing_ratio": points[metric]["missing_ratio"],
                                "max_missing_ratio": step["max_missing_ratio"],
                            }
                            for metric in missing
                        ],
                        "reset_consecutive_counts": True,
                    }
                    pause_evidence.append(evidence)
                paused_ticks += 1
                for metric in METRICS:
                    points[metric]["breached"] = False
                    points[metric]["consecutive_breaches"] = 0
                    points[metric]["adjudicated"] = False
                final_basis = _basis(now, points)
                continue

            if paused:
                paused = False
                evidence = {
                    "logical_time_seconds": now,
                    "reset_consecutive_counts": True,
                    "reason": "all_probe_missing_ratios_within_limit",
                }
                resume_evidence.append(evidence)

            for metric in METRICS:
                point = points[metric]
                breached = (
                    point["value"] is not None
                    and point["value"] > float(step["thresholds"][metric])
                )
                if breached:
                    breaches[metric] += 1
                    streak[metric] += 1
                elif streak[metric]:
                    spikes[metric] += 1
                    streak[metric] = 0
                point["breached"] = breached
                point["consecutive_breaches"] = streak[metric]
                point["adjudicated"] = True

            final_basis = _basis(now, points)
            sustained = [
                metric
                for metric in METRICS
                if streak[metric]
                >= int(step["required_consecutive_breaches"])
            ]
            if sustained:
                chosen = sustained[0]
                sustained_counts[chosen] += 1
                trigger = {
                    "kind": "sustained_metric_breach",
                    "metric": chosen,
                    "logical_time_seconds": now,
                    "threshold": step["thresholds"][chosen],
                    "observed": points[chosen]["value"],
                    "operator": ">",
                    "consecutive_breaches": streak[chosen],
                    "required_consecutive_breaches": int(
                        step["required_consecutive_breaches"]
                    ),
                    "window": points[chosen]["window"].copy(),
                    "sample_count": points[chosen]["sample_count"],
                    "used_sample_times": list(
                        points[chosen]["used_sample_times"]
                    ),
                }
                _close_runs(streak, spikes, keep=chosen)
                verdict = "rollback"
                reason = "sustained_metric_breach"
                break

        if verdict != "rollback":
            _close_runs(streak, spikes)
            if paused:
                verdict = "pause"
                reason = "probe_missing_at_window_end"
                trigger = {
                    "kind": "probe_missing_at_window_end",
                    "logical_time_seconds": expected[-1],
                    "metrics": pause_evidence[-1]["metrics"]
                    if pause_evidence
                    else [],
                }

        target = None
        if verdict == "rollback":
            target = _target(stable[-1] if stable else None)
            rollback_target = dict(target)
            stop_reason = (
                f"release_rolled_back_at_step_{step['step_number']}"
            )

        decision = {
            "decision_id": f"step-{step['step_number']}",
            "ledger_ref": f"step-{step['step_number']}",
            "step_number": step["step_number"],
            "step_id": step["step_id"],
            "declared_traffic_percent": step["traffic_percent"],
            "declared_traffic_ppm": step["traffic_ppm"],
            "verdict": verdict,
            "reason": reason,
            "trigger": trigger,
            "pause_evidence": pause_evidence,
            "resume_evidence": resume_evidence,
            "rollback_target": target,
            "metric_basis": final_basis,
            "reference_ledger": {
                "elapsed_window_seconds": executed_ticks * interval,
                "paused_seconds": paused_ticks * interval,
                "pause_count": pause_episodes,
                "rollback_count": 1 if verdict == "rollback" else 0,
                "metrics": {
                    metric: {
                        "breach_count": breaches[metric],
                        "spike_count": spikes[metric],
                        "sustained_breach_count": sustained_counts[metric],
                    }
                    for metric in METRICS
                },
            },
        }
        decisions.append(decision)
        if verdict == "promote":
            stable.append(decision)
        if verdict == "pause":
            stop_reason = f"release_paused_at_step_{step['step_number']}"

    return {
        "source_schema": project.get("source_schema"),
        "service_version": project["service_version"],
        "step_decisions": decisions,
        "rollback_target": rollback_target,
    }

