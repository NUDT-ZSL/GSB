from __future__ import annotations

from typing import Any, Mapping

from .validation import GatewayConfig


def decide_lock_mode(
    config: GatewayConfig,
    *,
    partition_id: str,
    backlog_depth: int,
    inflight_count: int,
    deliverable_offset: int,
    logical_clock_ms: int = 0,
) -> dict[str, Any]:
    backlog_hit = backlog_depth >= config.lock_switch_backlog_threshold
    inflight_hit = inflight_count >= config.lock_switch_inflight_threshold

    if not config.adaptive_lock_enabled:
        actual_mode = config.partition_lock_granularity
        trigger = "adaptive_disabled"
    elif backlog_hit or inflight_hit:
        actual_mode = "segment"
        triggers = []
        if backlog_hit:
            triggers.append(
                f"backlog_depth={backlog_depth}>=threshold={config.lock_switch_backlog_threshold}"
            )
        if inflight_hit:
            triggers.append(
                f"inflight_count={inflight_count}>=threshold={config.lock_switch_inflight_threshold}"
            )
        trigger = ";".join(triggers)
    else:
        actual_mode = "partition"
        trigger = (
            f"backlog_depth={backlog_depth}<threshold={config.lock_switch_backlog_threshold}"
            f";inflight_count={inflight_count}<threshold={config.lock_switch_inflight_threshold}"
        )

    rejected_mode = "partition" if actual_mode == "segment" else "segment"
    if actual_mode == "segment":
        reason = (
            "segment mode selected because backlog or concurrency exceeded its "
            "threshold; head-segment locking narrows lock scope while the "
            "partition head guard still enforces one in-flight delivery"
        )
    else:
        reason = (
            "partition mode retained because backlog and concurrency are below "
            "the switching thresholds; segment bookkeeping has no concurrency gain"
        )

    segment = deliverable_offset // config.segment_size
    return {
        "partition_id": partition_id,
        "actual_mode": actual_mode,
        "configured_mode": config.partition_lock_granularity,
        "rejected_mode": rejected_mode,
        "trigger": trigger,
        "reason": reason,
        "thresholds": {
            "backlog_depth": config.lock_switch_backlog_threshold,
            "inflight_count": config.lock_switch_inflight_threshold,
        },
        "observed": {
            "backlog_depth": backlog_depth,
            "inflight_count": inflight_count,
            "inflight_count_basis": "planned_round_concurrency",
            "deliverable_offset": deliverable_offset,
            "logical_clock_ms": logical_clock_ms,
        },
        "segment": segment,
        "segment_size": config.segment_size,
        "consistency_before": {
            "contiguous_offsets": True,
            "max_inflight_per_partition": 1,
        },
        "consistency_after": {
            "contiguous_offsets": True,
            "max_inflight_per_partition": 1,
        },
    }


def lock_key_for_decision(decision: Mapping[str, Any]) -> str:
    if decision["actual_mode"] == "segment":
        return (
            f"{decision['partition_id']}:segment:"
            f"{decision['segment']}"
        )
    return f"{decision['partition_id']}:partition"
