from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .canon import canonical_hash
from .watermark import partition_key


def schedule_diagnostics(
    bundle: Dict[str, Any],
    watermark_records: Dict[str, Dict[str, Any]],
    as_of_clock_ms: int,
    time_budget_ms: int,
    concurrency_per_partition: int,
    subscriber_filter: Optional[str] = None,
) -> Dict[str, Any]:
    if not isinstance(time_budget_ms, int) or isinstance(time_budget_ms, bool) or time_budget_ms < 0:
        raise ValueError("time_budget_ms must be a non-negative integer")
    if not isinstance(concurrency_per_partition, int) or isinstance(concurrency_per_partition, bool) or concurrency_per_partition <= 0:
        raise ValueError("concurrency_per_partition must be a positive integer")
    candidates: List[Dict[str, Any]] = []
    for subscription in bundle["subscriptions"]:
        if subscriber_filter is not None and subscription["subscriber_id"] != subscriber_filter:
            continue
        topic_name = subscription["topic"]
        topic_info = next(topic for topic in bundle["topics"] if topic["name"] == topic_name)
        for partition in range(topic_info["partitions"]):
            pk = partition_key(topic_name, partition)
            record = watermark_records[pk]
            interval_ms = max(1, math.ceil(1000 / subscription["rate_limit_per_sec"]))
            work_units = min(subscription["pull_batch_size"], max(1, record["event_count"]))
            cost_ms = work_units * interval_ms
            candidates.append(
                {
                    "subscriber_id": subscription["subscriber_id"],
                    "topic": topic_name,
                    "partition": partition,
                    "partition_key": pk,
                    "priority": subscription["priority"],
                    "cost_ms": cost_ms,
                    "work_units": work_units,
                    "rate_interval_ms": interval_ms,
                    "callback_url": subscription["callback_url"],
                }
            )
    candidates.sort(key=lambda item: (-item["priority"], item["subscriber_id"], item["partition"], item["partition_key"]))
    run_hash = canonical_hash(
        {
            "as_of_clock_ms": as_of_clock_ms,
            "time_budget_ms": time_budget_ms,
            "concurrency_per_partition": concurrency_per_partition,
            "subscriber_filter": subscriber_filter,
            "candidates": candidates,
            "watermark_hashes": {key: record["content_hash"] for key, record in sorted(watermark_records.items())},
        }
    )
    run_id = f"DIAG-RUN-{run_hash[:16]}"
    used_slots: Dict[str, int] = {}
    remaining_budget = time_budget_ms
    budget_cutoff = False
    tasks: List[Dict[str, Any]] = []
    counts = {
        "started_succeeded": 0,
        "started_failed": 0,
        "budget_unstarted": 0,
        "voluntarily_abandoned": 0,
    }
    outcome_class_counts = {
        "started_succeeded": 0,
        "failed_after_start": 0,
        "within_budget_unstarted": 0,
        "voluntarily_abandoned": 0,
    }
    budget_used = 0
    for stable_rank, candidate in enumerate(candidates):
        task_id = f"DT:{run_hash[:12]}:{candidate['subscriber_id']}:{candidate['partition']}"
        base = {
            "task_id": task_id,
            "run_id": run_id,
            "stable_rank": stable_rank,
            "subscriber_id": candidate["subscriber_id"],
            "topic": candidate["topic"],
            "partition": candidate["partition"],
            "partition_key": candidate["partition_key"],
            "priority": candidate["priority"],
            "logical_cost_ms": candidate["cost_ms"],
            "work_units": candidate["work_units"],
            "rate_interval_ms": candidate["rate_interval_ms"],
            "callback_url": candidate["callback_url"],
            "non_preemptive": True,
        }
        if budget_cutoff:
            task = {
                **base,
                "lifecycle": "abandoned",
                "end_category": "voluntarily_abandoned",
                "outcome_class": "voluntarily_abandoned",
                "label_zh": "主动放弃",
                "outcome": "abandoned_after_higher_priority_budget_cutoff",
                "reason": "remaining queue shed in subscriber priority and stable order after budget exhaustion",
            }
        elif used_slots.get(candidate["partition_key"], 0) >= concurrency_per_partition:
            task = {
                **base,
                "lifecycle": "abandoned",
                "end_category": "voluntarily_abandoned",
                "outcome_class": "voluntarily_abandoned",
                "label_zh": "主动放弃",
                "outcome": "abandoned_partition_concurrency_limit",
                "reason": "per-partition diagnostic delivery concurrency cap is full",
                "concurrency_cap": concurrency_per_partition,
            }
        elif candidate["cost_ms"] > remaining_budget:
            budget_cutoff = True
            task = {
                **base,
                "lifecycle": "unstarted",
                "end_category": "budget_unstarted",
                "outcome_class": "within_budget_unstarted",
                "label_zh": "预算内未启动",
                "outcome": "not_started_logical_time_budget_insufficient",
                "reason": "full non-preemptive task cost exceeds remaining logical time budget",
                "remaining_budget_ms": remaining_budget,
            }
        else:
            used_slots[candidate["partition_key"]] = used_slots.get(candidate["partition_key"], 0) + 1
            remaining_budget -= candidate["cost_ms"]
            budget_used += candidate["cost_ms"]
            record = watermark_records[candidate["partition_key"]]
            if record["diagnostic_outcome"] == "failed":
                task = {
                    **base,
                    "lifecycle": "started",
                    "end_category": "started_failed",
                    "outcome_class": "failed_after_start",
                    "label_zh": "启动后失败",
                    "outcome": "diagnostic_probe_failed_after_start",
                    "reason": (record.get("blocker") or {}).get("code", "WB_BACKLOG_LIMIT_EXCEEDED"),
                    "blocker": record.get("blocker"),
                    "started_and_completed": True,
                }
            else:
                task = {
                    **base,
                    "lifecycle": "started",
                    "end_category": "started_succeeded",
                    "outcome_class": "started_succeeded",
                    "label_zh": "启动成功",
                    "outcome": "diagnostic_probe_succeeded",
                    "reason": None,
                    "started_and_completed": True,
                }
        counts[task["end_category"]] += 1
        outcome_class_counts[task["outcome_class"]] += 1
        tasks.append(task)
    run = {
        "run_id": run_id,
        "run_hash": run_hash,
        "as_of_clock_ms": as_of_clock_ms,
        "time_budget_ms": time_budget_ms,
        "concurrency_per_partition": concurrency_per_partition,
        "subscriber_filter": subscriber_filter,
        "scheduling_policy": "priority_desc_then_subscriber_id_then_partition_stable",
        "non_preemptive_started_tasks": True,
        "budget_used_ms": budget_used,
        "budget_remaining_ms": remaining_budget,
        "counts": counts,
        "outcome_class_counts": outcome_class_counts,
        "tasks": tasks,
    }
    run["content_hash"] = canonical_hash({key: value for key, value in run.items() if key != "content_hash"})
    return run
