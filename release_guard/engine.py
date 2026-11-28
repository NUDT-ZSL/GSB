from __future__ import annotations

import math
from typing import Any


METRIC_ORDER = ("error_rate", "latency_p95_ms", "session_drop_rate")
METRIC_UNITS = {
    "error_rate": "fraction",
    "latency_p95_ms": "milliseconds",
    "session_drop_rate": "fraction",
}


def _expected_sample_times(step_start: int, step: dict[str, Any]) -> list[int]:
    interval = int(step["sample_interval_seconds"])
    end = step_start + int(step["observation_window_seconds"])
    return list(range(step_start + interval, end + 1, interval))


def _weighted_mean(pairs: list[tuple[float, int]]) -> float:
    total_weight = sum(weight for _, weight in pairs)
    return sum(value * weight for value, weight in pairs) / total_weight


def _weighted_nearest_rank(
    pairs: list[tuple[float, int]], quantile: float
) -> float:
    ordered = sorted(pairs, key=lambda item: item[0])
    total_weight = sum(weight for _, weight in ordered)
    rank = max(1, math.ceil(quantile * total_weight))
    cumulative = 0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= rank:
            return value
    return ordered[-1][0]


def _metric_value(
    metric: str, pairs: list[tuple[float, int]]
) -> float:
    if metric == "latency_p95_ms":
        return _weighted_nearest_rank(pairs, 0.95)
    return _weighted_mean(pairs)


def _rounded_metric(metric: str, value: float) -> float:
    if metric == "latency_p95_ms":
        return round(value, 3)
    return round(value, 9)


def _window_stats(
    *,
    step: dict[str, Any],
    step_start: int,
    expected_times: list[int],
    observations: dict[int, dict[str, Any]],
    timestamp: int,
    metric: str,
) -> dict[str, Any]:
    start_exclusive = max(
        step_start,
        timestamp - int(step["sliding_window_seconds"]),
        timestamp - int(step["observation_window_seconds"]),
    )
    window_times = [
        item for item in expected_times if start_exclusive < item <= timestamp
    ]
    present_pairs: list[tuple[float, int]] = []
    present_times: list[int] = []
    for item in window_times:
        sample = observations.get(item)
        if sample is None:
            continue
        raw_value = sample.get(metric)
        if raw_value is None:
            continue
        present_pairs.append((float(raw_value), int(sample["sample_count"])))
        present_times.append(item)

    expected_count = len(window_times)
    present_count = len(present_pairs)
    missing_count = expected_count - present_count
    missing_ratio = missing_count / expected_count if expected_count else 0.0
    degraded = missing_ratio > float(step["max_missing_ratio"])
    if degraded:
        value = None
        sample_count = 0
        used_sample_times: list[int] = []
    else:
        value = _rounded_metric(metric, _metric_value(metric, present_pairs))
        sample_count = sum(weight for _, weight in present_pairs)
        used_sample_times = present_times

    return {
        "metric": metric,
        "unit": METRIC_UNITS[metric],
        "window": {
            "start_exclusive": start_exclusive,
            "end_inclusive": timestamp,
        },
        "expected_sample_points": expected_count,
        "present_sample_points": present_count,
        "missing_sample_points": missing_count,
        "missing_ratio": round(missing_ratio, 9),
        "sample_count": sample_count,
        "used_sample_times": used_sample_times,
        "value": value,
        "missing_degraded": degraded,
    }


def _basis(
    timestamp: int, points: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    metrics = {}
    for metric in METRIC_ORDER:
        point = points[metric]
        metrics[metric] = {
            "window": dict(point["window"]),
            "sample_count": point["sample_count"],
            "value": point["value"],
            "threshold": point["threshold"],
            "breached": point["breached"],
            "consecutive_breaches": point["consecutive_breaches"],
            "missing_ratio": point["missing_ratio"],
        }
    return {"logical_time_seconds": timestamp, "metrics": metrics}


def _close_active_breaches(
    streaks: dict[str, int],
    spikes: dict[str, int],
    exclude: str | None = None,
) -> None:
    for metric in METRIC_ORDER:
        if metric == exclude:
            continue
        if streaks[metric] > 0:
            spikes[metric] += 1
            streaks[metric] = 0


def _zero_metric_counts() -> dict[str, dict[str, int]]:
    return {
        metric: {
            "breach_count": 0,
            "spike_count": 0,
            "sustained_breach_count": 0,
        }
        for metric in METRIC_ORDER
    }


def _target_descriptor(decision: dict[str, Any] | None) -> dict[str, Any]:
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


def _session_dispositions(
    capacity: int, current_ppm: int, target_ppm: int
) -> tuple[int, list[dict[str, Any]]]:
    affected = (
        capacity * (current_ppm - target_ppm) + 500_000
    ) // 1_000_000
    if affected <= 0:
        return 0, []
    priority = affected // 4
    live = affected // 4
    standard = affected - priority - live
    groups = [
        ("priority-exam-sessions", priority, "migrate_with_answer_checkpoint"),
        ("live-proctoring-sessions", live, "migrate_with_media_reconnect"),
        ("standard-exam-sessions", standard, "migrate_and_resume_checkpoint"),
    ]
    dispositions = []
    first = 1
    for cohort, count, disposition in groups:
        if count <= 0:
            continue
        last = first + count - 1
        dispositions.append(
            {
                "cohort": cohort,
                "count": count,
                "session_range": f"S{first:06d}-S{last:06d}",
                "disposition": disposition,
            }
        )
        first = last + 1
    return affected, dispositions


def _build_rollback_action(
    *,
    run: dict[str, Any],
    failed_step: dict[str, Any],
    failed_decision: dict[str, Any],
    target_decision: dict[str, Any] | None,
    target_step: dict[str, Any] | None,
    trigger: dict[str, Any],
) -> dict[str, Any]:
    current_ppm = int(failed_step["traffic_ppm"])
    target_ppm = int(target_step["traffic_ppm"]) if target_step else 0
    target = _target_descriptor(target_decision)
    recovered_ppm = current_ppm - target_ppm
    affected, sessions = _session_dispositions(
        int(run["session_capacity"]), current_ppm, target_ppm
    )
    rollback_time = trigger["logical_time_seconds"]
    if target_step is not None:
        reobserve = {
            "logical_start_seconds": rollback_time,
            "logical_end_seconds": (
                rollback_time + int(target_step["observation_window_seconds"])
            ),
            "duration_seconds": int(target_step["observation_window_seconds"]),
            "sliding_window_seconds": int(target_step["sliding_window_seconds"]),
            "sample_interval_seconds": int(target_step["sample_interval_seconds"]),
            "required_consecutive_breaches": int(
                target_step["required_consecutive_breaches"]
            ),
            "thresholds": dict(target_step["thresholds"]),
        }
    else:
        reobserve = None

    actions = [
        {
            "sequence": 1,
            "action": "freeze_rollout",
            "logical_time_seconds": rollback_time,
            "detail": "停止继续放量并冻结新会话进入候选版本",
        },
        {
            "sequence": 2,
            "action": "switch_traffic",
            "logical_time_seconds": rollback_time,
            "from_percent": failed_step["traffic_percent"],
            "to_percent": (
                target_step["traffic_percent"] if target_step else "0"
            ),
            "recovered_ppm": recovered_ppm,
            "detail": "将超出的流量切回最近已通过判定的稳定步骤",
        },
        {
            "sequence": 3,
            "action": "drain_candidate_sessions",
            "logical_time_seconds": rollback_time,
            "affected_session_count": affected,
            "detail": "按会话清单迁移并恢复答题检查点",
        },
        {
            "sequence": 4,
            "action": "reobserve_stable_step",
            "logical_time_seconds": rollback_time,
            "window": reobserve,
            "detail": "回退后重新计入目标步骤的完整观察窗口",
        },
        {
            "sequence": 5,
            "action": "mark_release_rolled_back",
            "logical_time_seconds": rollback_time,
            "detail": "候选版本保持冻结，等待人工复核",
        },
    ]
    return {
        "required": True,
        "failed_step": {
            "step_number": failed_step["step_number"],
            "step_id": failed_step["step_id"],
            "traffic_percent": failed_step["traffic_percent"],
        },
        "target": target,
        "trigger": trigger,
        "traffic_recovery_ppm": recovered_ppm,
        "traffic_recovery_percent": _percent_text(recovered_ppm),
        "traffic_after_rollback_percent": target["traffic_percent"],
        "reobserve_window": reobserve,
        "affected_session_count": affected,
        "affected_session_dispositions": sessions,
        "actions": actions,
    }


def _percent_text(ppm: int) -> str:
    value = ppm / 10000
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _not_evaluated_decision(
    step: dict[str, Any], stop_reason: str
) -> dict[str, Any]:
    decision_id = f"step-{step['step_number']}"
    return {
        "decision_id": decision_id,
        "ledger_ref": decision_id,
        "step_number": step["step_number"],
        "step_id": step["step_id"],
        "declared_traffic_percent": step["traffic_percent"],
        "declared_traffic_ppm": step["traffic_ppm"],
        "verdict": "not_evaluated",
        "reason": stop_reason,
        "trigger": None,
        "pause_evidence": [],
        "resume_evidence": [],
        "rollback_target": None,
        "metric_basis": None,
    }


def _not_evaluated_ledger(step: dict[str, Any]) -> dict[str, Any]:
    step_id = f"step-{step['step_number']}"
    return {
        "entry_id": step_id,
        "decision_ref": step_id,
        "step_number": step["step_number"],
        "step_id": step["step_id"],
        "declared_traffic_percent": step["traffic_percent"],
        "declared_traffic_ppm": step["traffic_ppm"],
        "verdict": "not_evaluated",
        "elapsed_window_seconds": 0,
        "actual_rollout_seconds": 0,
        "paused_seconds": 0,
        "pause_count": 0,
        "rollback_count": 0,
        "metrics": _zero_metric_counts(),
    }


def run_main(normalized: dict[str, Any]) -> dict[str, Any]:
    """Execute the main progressive-release adjudicator."""
    steps = normalized["steps"]
    observations = {
        int(item["logical_time_seconds"]): item
        for item in normalized["observations"]
    }
    step_starts: dict[int, int] = {}
    offset = 0
    for step in steps:
        step_starts[int(step["step_number"])] = offset
        offset += int(step["observation_window_seconds"])

    decisions: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    trajectories: dict[str, dict[str, list[dict[str, Any]]]] = {}
    action_events: list[dict[str, Any]] = []
    rollback_action: dict[str, Any] | None = None
    promoted: list[dict[str, Any]] = []
    stop_reason: str | None = None
    rollback_target_summary: dict[str, Any] | None = None
    event_sequence = 0

    for step in steps:
        if stop_reason is not None:
            decisions.append(_not_evaluated_decision(step, stop_reason))
            ledger.append(_not_evaluated_ledger(step))
            continue

        step_number = int(step["step_number"])
        step_id = f"step-{step_number}"
        start = step_starts[step_number]
        expected_times = _expected_sample_times(start, step)
        thresholds = step["thresholds"]
        streaks = {metric: 0 for metric in METRIC_ORDER}
        breaches = {metric: 0 for metric in METRIC_ORDER}
        spikes = {metric: 0 for metric in METRIC_ORDER}
        sustained = {metric: 0 for metric in METRIC_ORDER}
        trajectory = {metric: [] for metric in METRIC_ORDER}
        pause_evidence: list[dict[str, Any]] = []
        resume_evidence: list[dict[str, Any]] = []
        paused = False
        pause_seconds = 0
        executed_ticks = 0
        pause_episode_count = 0
        final_trigger: dict[str, Any] | None = None
        last_basis: dict[str, Any] | None = None
        verdict = "promote"
        reason = "observation_window_completed_without_sustained_breach"

        for tick_number, timestamp in enumerate(expected_times, start=1):
            executed_ticks += 1
            metric_windows = {
                metric: _window_stats(
                    step=step,
                    step_start=start,
                    expected_times=expected_times,
                    observations=observations,
                    timestamp=timestamp,
                    metric=metric,
                )
                for metric in METRIC_ORDER
            }
            degraded_metrics = [
                metric
                for metric in METRIC_ORDER
                if metric_windows[metric]["missing_degraded"]
            ]
            points: dict[str, dict[str, Any]] = {}

            if degraded_metrics:
                _close_active_breaches(streaks, spikes)
                started_new_pause = False
                if not paused:
                    paused = True
                    pause_episode_count += 1
                    started_new_pause = True
                    event_sequence += 1
                    evidence = {
                        "kind": "probe_missing",
                        "logical_time_seconds": timestamp,
                        "window": dict(metric_windows[METRIC_ORDER[0]]["window"]),
                        "metrics": [
                            {
                                "metric": metric,
                                "expected_points": metric_windows[metric][
                                    "expected_sample_points"
                                ],
                                "present_points": metric_windows[metric][
                                    "present_sample_points"
                                ],
                                "missing_ratio": metric_windows[metric][
                                    "missing_ratio"
                                ],
                                "max_missing_ratio": step["max_missing_ratio"],
                            }
                            for metric in degraded_metrics
                        ],
                        "reset_consecutive_counts": True,
                    }
                    pause_evidence.append(evidence)
                    action_events.append(
                        {
                            "sequence": event_sequence,
                            "step_number": step_number,
                            "logical_time_seconds": timestamp,
                            "action": "pause",
                            "evidence": evidence,
                        }
                    )
                pause_seconds += int(step["sample_interval_seconds"])
                outcome = "pause" if started_new_pause else "pause_hold"

                for metric in METRIC_ORDER:
                    stats = metric_windows[metric]
                    value = stats["value"]
                    would_breach = (
                        value is not None and value > float(thresholds[metric])
                    )
                    points[metric] = {
                        **stats,
                        "threshold": thresholds[metric],
                        "operator": ">",
                        "breached": False,
                        "would_breach": would_breach,
                        "consecutive_breaches": 0,
                        "adjudicated": False,
                    }
                    trajectory[metric].append(
                        {
                            "tick": tick_number,
                            **points[metric],
                        }
                    )
                last_basis = _basis(timestamp, points)
                continue

            resumed = paused
            if resumed:
                paused = False
                event_sequence += 1
                evidence = {
                    "logical_time_seconds": timestamp,
                    "reset_consecutive_counts": True,
                    "reason": "all_probe_missing_ratios_within_limit",
                }
                resume_evidence.append(evidence)
                action_events.append(
                    {
                        "sequence": event_sequence,
                        "step_number": step_number,
                        "logical_time_seconds": timestamp,
                        "action": "resume",
                        "evidence": evidence,
                    }
                )

            for metric in METRIC_ORDER:
                stats = metric_windows[metric]
                value = stats["value"]
                above = value is not None and value > float(thresholds[metric])
                if above:
                    breaches[metric] += 1
                    streaks[metric] += 1
                elif streaks[metric] > 0:
                    spikes[metric] += 1
                    streaks[metric] = 0
                points[metric] = {
                    **stats,
                    "threshold": thresholds[metric],
                    "operator": ">",
                    "breached": above,
                    "would_breach": above,
                    "consecutive_breaches": streaks[metric],
                    "adjudicated": True,
                }
                trajectory[metric].append(
                    {
                        "tick": tick_number,
                        **points[metric],
                    }
                )

            sustained_metric = next(
                (
                    metric
                    for metric in METRIC_ORDER
                    if streaks[metric]
                    >= int(step["required_consecutive_breaches"])
                ),
                None,
            )
            if sustained_metric is not None:
                sustained[sustained_metric] += 1
                point = points[sustained_metric]
                final_trigger = {
                    "kind": "sustained_metric_breach",
                    "metric": sustained_metric,
                    "logical_time_seconds": timestamp,
                    "threshold": thresholds[sustained_metric],
                    "observed": point["value"],
                    "operator": ">",
                    "consecutive_breaches": streaks[sustained_metric],
                    "required_consecutive_breaches": int(
                        step["required_consecutive_breaches"]
                    ),
                    "window": dict(point["window"]),
                    "sample_count": point["sample_count"],
                    "used_sample_times": list(point["used_sample_times"]),
                }
                _close_active_breaches(
                    streaks, spikes, exclude=sustained_metric
                )
                verdict = "rollback"
                reason = "sustained_metric_breach"
                last_basis = _basis(timestamp, points)
                event_sequence += 1
                action_events.append(
                    {
                        "sequence": event_sequence,
                        "step_number": step_number,
                        "logical_time_seconds": timestamp,
                        "action": "rollback",
                        "evidence": final_trigger,
                    }
                )
                break

            last_basis = _basis(timestamp, points)
            outcome = "resume" if resumed else "continue"
            action_events.append(
                {
                    "sequence": 0,
                    "step_number": step_number,
                    "logical_time_seconds": timestamp,
                    "action": outcome,
                    "evidence": None,
                }
            )

        if verdict != "rollback":
            _close_active_breaches(streaks, spikes)
            if paused:
                verdict = "pause"
                reason = "probe_missing_at_window_end"
                final_trigger = {
                    "kind": "probe_missing_at_window_end",
                    "logical_time_seconds": (
                        expected_times[-1] if expected_times else step_starts[step_number]
                    ),
                    "metrics": pause_evidence[-1]["metrics"]
                    if pause_evidence
                    else [],
                }

        rollback_target = None
        if verdict == "rollback":
            target_decision = promoted[-1] if promoted else None
            target_step_number = (
                int(target_decision["step_number"]) if target_decision else None
            )
            target_step = (
                steps[target_step_number - 1] if target_step_number else None
            )
            rollback_target = _target_descriptor(target_decision)
            rollback_action = _build_rollback_action(
                run=normalized,
                failed_step=step,
                failed_decision={},
                target_decision=target_decision,
                target_step=target_step,
                trigger=final_trigger,
            )
            rollback_target_summary = dict(rollback_target)
            stop_reason = f"release_rolled_back_at_step_{step_number}"

        decision = {
            "decision_id": step_id,
            "ledger_ref": step_id,
            "step_number": step_number,
            "step_id": step["step_id"],
            "declared_traffic_percent": step["traffic_percent"],
            "declared_traffic_ppm": step["traffic_ppm"],
            "verdict": verdict,
            "reason": reason,
            "trigger": final_trigger,
            "pause_evidence": pause_evidence,
            "resume_evidence": resume_evidence,
            "rollback_target": rollback_target,
            "metric_basis": last_basis,
        }
        decisions.append(decision)
        if verdict == "promote":
            promoted.append(decision)

        elapsed = executed_ticks * int(step["sample_interval_seconds"])
        actual_rollout = elapsed - pause_seconds
        ledger.append(
            {
                "entry_id": step_id,
                "decision_ref": step_id,
                "step_number": step_number,
                "step_id": step["step_id"],
                "declared_traffic_percent": step["traffic_percent"],
                "declared_traffic_ppm": step["traffic_ppm"],
                "verdict": verdict,
                "elapsed_window_seconds": elapsed,
                "actual_rollout_seconds": actual_rollout,
                "paused_seconds": pause_seconds,
                "pause_count": pause_episode_count,
                "rollback_count": 1 if verdict == "rollback" else 0,
                "metrics": {
                    metric: {
                        "breach_count": breaches[metric],
                        "spike_count": spikes[metric],
                        "sustained_breach_count": sustained[metric],
                    }
                    for metric in METRIC_ORDER
                },
            }
        )
        trajectories[step_id] = trajectory
        if verdict in ("rollback", "pause"):
            stop_reason = (
                f"release_rolled_back_at_step_{step_number}"
                if verdict == "rollback"
                else f"release_paused_at_step_{step_number}"
            )

    # Continue events are useful in the timeline but not in the action ledger.
    notable_events = [
        event for event in action_events if event["sequence"] > 0
    ]
    for sequence, event in enumerate(notable_events, start=1):
        event["sequence"] = sequence

    totals = {
        "declared_steps": len(steps),
        "evaluated_steps": sum(
            1 for item in decisions if item["verdict"] != "not_evaluated"
        ),
        "promoted_steps": len(promoted),
        "pause_count": sum(item["pause_count"] for item in ledger),
        "rollback_count": sum(item["rollback_count"] for item in ledger),
        "breach_count": sum(
            metric["breach_count"]
            for item in ledger
            for metric in item["metrics"].values()
        ),
        "spike_count": sum(
            metric["spike_count"]
            for item in ledger
            for metric in item["metrics"].values()
        ),
        "sustained_breach_count": sum(
            metric["sustained_breach_count"]
            for item in ledger
            for metric in item["metrics"].values()
        ),
    }
    highest = max(
        (
            item["declared_traffic_percent"]
            for item, decision in zip(ledger, decisions)
            if decision["verdict"] != "not_evaluated"
        ),
        key=lambda value: float(value),
        default="0",
    )
    return {
        "source_schema": normalized.get("source_schema"),
        "service_version": normalized["service_version"],
        "step_decisions": decisions,
        "metric_trajectories": trajectories,
        "rollback": rollback_action,
        "rollback_target": rollback_target_summary,
        "action_events": notable_events,
        "ledger": ledger,
        "summary": {
            **totals,
            "highest_evaluated_traffic_percent": highest,
            "stopped": stop_reason is not None,
            "stop_reason": stop_reason,
        },
    }

