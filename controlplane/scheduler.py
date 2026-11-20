"""Logical-time, bounded-concurrency dispatch simulation."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Mapping

from .validation import Sample

SUCCEEDED = "SUCCEEDED"
STARTED_FAILED = "STARTED_FAILED"
NOT_STARTED = "NOT_STARTED"
ACTIVE_ABANDONED = "ACTIVE_ABANDONED"

OUTCOME_ORDER = (
    SUCCEEDED,
    STARTED_FAILED,
    NOT_STARTED,
    ACTIVE_ABANDONED,
)


@dataclass
class TaskState:
    cluster: str
    region: str
    priority: int
    latency_ms: int
    fail_attempts: int
    order: int
    rank: Fraction
    outcome: str | None = None
    reason: str | None = None
    started_at: int | None = None
    finished_at: int | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)


def _result(
    task: TaskState,
    status: str,
    reason: str | None,
    *,
    started_at: int | None,
    finished_at: int | None,
) -> dict[str, Any]:
    return {
        "cluster": task.cluster,
        "region": task.region,
        "priority": task.priority,
        "status": status,
        "reason": reason,
        "started": started_at is not None,
        "started_at_ms": started_at,
        "finished_at_ms": finished_at,
        "attempts": task.attempts,
    }


def _rank(result: Mapping[str, Any]) -> Fraction:
    rank = result.get("dispatch_rank")
    if not rank:
        return Fraction(0, 1)
    return Fraction(int(rank["fixed"]), int(rank["scale"]))


def _task_sort_key(task: TaskState) -> tuple[int, Fraction, int]:
    return (-task.priority, -task.rank, task.order)


def run_scheduler(
    sample: Sample,
    cluster_results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    deadline = sample.policy.logical_time_budget_ms
    max_parallel = sample.policy.max_parallel_per_round
    retry_budget = sample.policy.retry_budget
    current_time = 0
    max_observed = 0
    heaps: list[tuple[int, int, TaskState, int]] = []
    outcomes: dict[str, dict[str, Any]] = {}
    all_events: list[dict[str, Any]] = []
    event_seq = 0
    order_seq = 0
    region_groups: dict[str, dict[str, int]] = {}

    def record_event(time_ms: int, cluster: str, event: str, **extra: Any) -> None:
        nonlocal event_seq
        payload = {
            "seq": event_seq,
            "time_ms": time_ms,
            "cluster": cluster,
            "event": event,
        }
        payload.update(extra)
        all_events.append(payload)
        event_seq += 1

    for wave in sample.region_waves:
        ready_tasks: list[TaskState] = []
        for region in wave:
            region_clusters = sorted(
                (
                    result
                    for result in cluster_results.values()
                    if result["region"] == region
                ),
                key=lambda item: (
                    -int(item["priority"]),
                    -_rank(item),
                    item["cluster"],
                ),
            )
            for result in region_clusters:
                task = TaskState(
                    cluster=result["cluster"],
                    region=region,
                    priority=int(result["priority"]),
                    latency_ms=int(result["dispatch_profile"]["latency_ms"]),
                    fail_attempts=int(result["dispatch_profile"]["fail_attempts"]),
                    order=order_seq,
                    rank=_rank(result),
                )
                order_seq += 1
                if not result["dispatch_eligible"]:
                    reason_codes = [
                        reason["code"]
                        for reason in result["dispatch_ineligibility_reasons"]
                    ]
                    task.outcome = ACTIVE_ABANDONED
                    task.reason = ",".join(sorted(set(reason_codes)))
                    task.started_at = None
                    task.finished_at = current_time
                    outcomes[task.cluster] = _result(
                        task,
                        ACTIVE_ABANDONED,
                        task.reason,
                        started_at=None,
                        finished_at=current_time,
                    )
                    record_event(
                        current_time,
                        task.cluster,
                        "ACTIVE_ABANDONED",
                        reason=task.reason,
                    )
                else:
                    ready_tasks.append(task)
        ready_tasks.sort(key=lambda item: (item.region, *_task_sort_key(item)))
        attempt_numbers: dict[str, int] = {task.cluster: 1 for task in ready_tasks}
        pending: list[TaskState] = list(ready_tasks)

        while pending or heaps:
            while (
                pending
                and len(heaps) < max_parallel
                and current_time < deadline
            ):
                task = pending.pop(0)
                attempt = attempt_numbers[task.cluster]
                finish = current_time + task.latency_ms
                task.attempts.append(
                    {
                        "attempt": attempt,
                        "started_at_ms": current_time,
                        "scheduled_finish_at_ms": finish,
                        "started_before_deadline": True,
                    }
                )
                if task.started_at is None:
                    task.started_at = current_time
                heapq.heappush(heaps, (finish, task.order, task, attempt))
                record_event(
                    current_time,
                    task.cluster,
                    "ATTEMPT_STARTED",
                    attempt=attempt,
                    scheduled_finish_at_ms=finish,
                )
                max_observed = max(max_observed, len(heaps))

            if not heaps:
                if pending and current_time >= deadline:
                    for task in pending:
                        task.outcome = NOT_STARTED
                        task.reason = "time_budget_exhausted_before_start"
                        task.finished_at = current_time
                        outcomes[task.cluster] = _result(
                            task,
                            NOT_STARTED,
                            task.reason,
                            started_at=None,
                            finished_at=current_time,
                        )
                        record_event(
                            current_time,
                            task.cluster,
                            "NOT_STARTED",
                            reason=task.reason,
                        )
                    pending.clear()
                break

            finish_time, _, task, attempt = heapq.heappop(heaps)
            current_time = finish_time
            attempt_record = task.attempts[-1]
            will_fail = attempt <= task.fail_attempts
            attempt_record["finished_at_ms"] = finish_time
            attempt_record["result"] = "FAILED" if will_fail else "SUCCEEDED"
            if not will_fail:
                task.outcome = SUCCEEDED
                task.reason = None
                task.finished_at = finish_time
                outcomes[task.cluster] = _result(
                    task,
                    SUCCEEDED,
                    None,
                    started_at=task.started_at,
                    finished_at=finish_time,
                )
                record_event(
                    finish_time,
                    task.cluster,
                    "ATTEMPT_SUCCEEDED",
                    attempt=attempt,
                )
                continue

            record_event(
                finish_time,
                task.cluster,
                "ATTEMPT_FAILED",
                attempt=attempt,
            )
            retry_available = attempt - 1 < retry_budget
            if retry_available:
                attempt_numbers[task.cluster] = attempt + 1
                pending.append(task)
                pending.sort(
                    key=lambda item: (item.region, *_task_sort_key(item))
                )
                if current_time >= deadline:
                    task.outcome = STARTED_FAILED
                    task.reason = "retry_not_started_time_budget_exhausted"
                    task.finished_at = finish_time
                    outcomes[task.cluster] = _result(
                        task,
                        STARTED_FAILED,
                        task.reason,
                        started_at=task.started_at,
                        finished_at=finish_time,
                    )
                    pending.remove(task)
                    record_event(
                        finish_time,
                        task.cluster,
                        "STARTED_FAILED",
                        reason=task.reason,
                    )
            else:
                task.outcome = STARTED_FAILED
                task.reason = "retry_budget_exhausted"
                task.finished_at = finish_time
                outcomes[task.cluster] = _result(
                    task,
                    STARTED_FAILED,
                    task.reason,
                    started_at=task.started_at,
                    finished_at=finish_time,
                )
                record_event(
                    finish_time,
                    task.cluster,
                    "STARTED_FAILED",
                    reason=task.reason,
                )

        for result in cluster_results.values():
            if result["region"] in wave:
                group = region_groups.setdefault(
                    result["region"],
                    {status: 0 for status in OUTCOME_ORDER},
                )
                outcome = outcomes.get(result["cluster"])
                if outcome:
                    group[outcome["status"]] += 1

    counts = {status: 0 for status in OUTCOME_ORDER}
    for outcome in outcomes.values():
        counts[outcome["status"]] += 1

    ordered_outcomes = {
        cluster: outcomes[cluster] for cluster in sorted(outcomes)
    }
    return {
        "deadline_ms": deadline,
        "max_parallel_per_round": max_parallel,
        "retry_budget": retry_budget,
        "max_observed_concurrency": max_observed,
        "finished_at_ms": current_time,
        "budget_exhausted": counts[NOT_STARTED] > 0,
        "counts": counts,
        "outcomes": ordered_outcomes,
        "region_groups": {
            region: region_groups[region]
            for region in sorted(region_groups)
        },
        "events": sorted(
            all_events,
            key=lambda item: (item["time_ms"], item["seq"]),
        ),
    }
