"""Budgeted, deadline-aware deterministic subtask arbitration."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .cost import DriftEvent
from .errors import BusinessError, INVALID_ARGUMENT, RESOURCE_OVERFLOW
from .models import Candidate, Plan
from .subtasks import Subtask, SubtaskChain
from .util import digest_obj


TERMINAL_SUCCESS = "committed"
TERMINAL_DEADLINE = "completed_deadline_miss"
TERMINAL_FAILED = "failed_after_start"
TERMINAL_BLOCKED = "blocked_anomaly"
TERMINAL_ABANDONED = "abandoned_budget"
TERMINAL_DEPENDENCY = "blocked_dependency"


@dataclass(frozen=True)
class ScheduleConfig:
    budget: float
    max_concurrency: int
    rounds: int
    start_clock_ms: int
    round_span_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget,
            "max_concurrency": self.max_concurrency,
            "rounds": self.rounds,
            "start_clock_ms": self.start_clock_ms,
            "round_span_ms": self.round_span_ms,
        }


def _job_deadline(candidate: Candidate, job_id: str) -> float:
    return candidate.jobs[job_id].deadline_clock_ms


def _job_priority(candidate: Candidate, job_id: str) -> int:
    return candidate.jobs[job_id].priority


def _descendant_jobs(candidate: Candidate, job_ids: Iterable[str]) -> set[str]:
    reverse: dict[str, set[str]] = {job_id: set() for job_id in candidate.jobs}
    for job_id, job in candidate.jobs.items():
        for dep in job.dependencies:
            reverse[dep].add(job_id)
    affected = set(job_ids)
    queue = list(sorted(affected))
    while queue:
        current = queue.pop(0)
        for dependent in sorted(reverse.get(current, ())):
            if dependent not in affected:
                affected.add(dependent)
                queue.append(dependent)
    return affected


def _critical_path_members(
    plan: Plan, candidate: Candidate, job_id: str
) -> set[str]:
    path_id = candidate.jobs[job_id].critical_path_id
    if path_id and path_id in plan.critical_paths:
        return set(plan.critical_paths[path_id])
    for members in plan.critical_paths.values():
        if job_id in members:
            return set(members)
    return {job_id} | _descendant_jobs(candidate, [job_id])


def _subtask_sort_key(
    subtask: Subtask, candidate: Candidate
) -> tuple[Any, ...]:
    job = candidate.jobs[subtask.job_id]
    return (
        job.priority,
        job.deadline_clock_ms,
        subtask.job_id,
        subtask.ordinal,
        subtask.subtask_id,
    )


def _cost_map(cost_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        step["subtask_id"]: step
        for step in cost_result.get("steps", [])
    }


def _anomalies_for_subtask(
    events: Iterable[DriftEvent], subtask_id: str
) -> list[DriftEvent]:
    return [
        event
        for event in events
        if event.anomaly_type is not None
        and (event.subtask_id == subtask_id or event.subtask_id is None)
    ]


def schedule_plan(
    *,
    plan: Plan,
    candidate: Candidate,
    chains: dict[str, SubtaskChain],
    cost_result: dict[str, Any],
    events: Iterable[DriftEvent],
    config: ScheduleConfig,
) -> dict[str, Any]:
    if config.budget < 0 or not math.isfinite(config.budget):
        raise BusinessError(
            INVALID_ARGUMENT, "budget must be non-negative and finite"
        )
    if config.max_concurrency < 1:
        raise BusinessError(
            INVALID_ARGUMENT, "max_concurrency must be at least one"
        )
    if config.rounds < 1:
        raise BusinessError(INVALID_ARGUMENT, "rounds must be at least one")
    if config.start_clock_ms < 0 or config.round_span_ms < 1:
        raise BusinessError(
            INVALID_ARGUMENT,
            "start_clock_ms must be non-negative and round_span_ms positive",
        )

    all_subtasks = [
        subtask
        for job_id in sorted(chains)
        for subtask in chains[job_id].subtasks
    ]
    by_id = {subtask.subtask_id: subtask for subtask in all_subtasks}
    costs = _cost_map(cost_result)
    missing_cost = sorted(set(by_id) - set(costs))
    if missing_cost:
        raise BusinessError(
            INVALID_ARGUMENT,
            "cost forecast is missing subtasks",
            details={"subtask_ids": missing_cost},
        )

    status: dict[str, str] = {subtask_id: "pending" for subtask_id in by_id}
    results: dict[str, dict[str, Any]] = {}
    anomalies: dict[str, dict[str, Any]] = {}
    rounds_output: list[dict[str, Any]] = []

    def add_anomaly(
        *,
        kind: str,
        subtask: Subtask,
        source_event: DriftEvent | None,
        message: str,
        persistent: bool,
    ) -> str:
        anomaly_id = "anom_" + digest_obj(
            {
                "kind": kind,
                "plan_version": plan.plan_version,
                "candidate_id": candidate.candidate_id,
                "job_id": subtask.job_id,
                "subtask_id": subtask.subtask_id,
                "event_id": source_event.event_id if source_event else None,
            }
        )
        anomalies[anomaly_id] = {
            "anomaly_id": anomaly_id,
            "kind": kind,
            "job_id": subtask.job_id,
            "subtask_id": subtask.subtask_id,
            "event_id": source_event.event_id if source_event else None,
            "message": message,
            "persistent": persistent,
            "logical_clock_ms": (
                source_event.logical_clock_ms if source_event else config.start_clock_ms
            ),
        }
        return anomaly_id

    def block_subtask(
        *,
        subtask: Subtask,
        reason: str,
        kind: str,
        source_event: DriftEvent | None,
        message: str,
    ) -> None:
        anomaly_id = add_anomaly(
            kind=kind,
            subtask=subtask,
            source_event=source_event,
            message=message,
            persistent=True,
        )
        status[subtask.subtask_id] = TERMINAL_BLOCKED
        results[subtask.subtask_id] = {
            "subtask_id": subtask.subtask_id,
            "job_id": subtask.job_id,
            "ordinal": subtask.ordinal,
            "phase": subtask.phase,
            "status": TERMINAL_BLOCKED,
            "reason": reason,
            "started": False,
            "start_clock_ms": None,
            "end_clock_ms": None,
            "cost": costs[subtask.subtask_id],
            "event_id": source_event.event_id if source_event else None,
            "anomaly_id": anomaly_id,
        }

    # Structural and injected anomalies are dispositions, not launch
    # candidates.  Scan all nodes first so a low concurrency cap cannot hide
    # an over-cap or deadlock-marked node.
    for subtask in sorted(
        all_subtasks, key=lambda item: _subtask_sort_key(item, candidate)
    ):
        job = candidate.jobs[subtask.job_id]
        injected = _anomalies_for_subtask(events, subtask.subtask_id)
        blocked_event = next(
            (
                event
                for event in injected
                if event.anomaly_type in ("deadlock", "resource_overrun")
            ),
            None,
        )
        if job.threads_required > plan.max_threads:
            block_subtask(
                subtask=subtask,
                reason="resource_overrun",
                kind=RESOURCE_OVERFLOW,
                source_event=None,
                message=(
                    f"threads_required={job.threads_required} exceeds "
                    f"max_threads={plan.max_threads}"
                ),
            )
        elif blocked_event is not None:
            block_subtask(
                subtask=subtask,
                reason=blocked_event.anomaly_type or "anomaly",
                kind=(
                    "deadlock"
                    if blocked_event.anomaly_type == "deadlock"
                    else RESOURCE_OVERFLOW
                ),
                source_event=blocked_event,
                message=f"injected {blocked_event.anomaly_type} anomaly",
            )

    for round_index in range(config.rounds):
        round_clock = config.start_clock_ms + round_index * config.round_span_ms
        remaining = config.budget
        slots = config.max_concurrency
        started: list[str] = []
        deferred_budget: list[str] = []

        def ready_subtasks() -> list[Subtask]:
            ready: list[Subtask] = []
            for subtask in all_subtasks:
                if status[subtask.subtask_id] != "pending":
                    continue
                if all(
                    status.get(dep) == TERMINAL_SUCCESS
                    for dep in subtask.dependencies
                ):
                    ready.append(subtask)
            return sorted(ready, key=lambda item: _subtask_sort_key(item, candidate))

        while slots > 0:
            ready = ready_subtasks()
            if not ready:
                break
            selected: Subtask | None = None
            selected_cost: dict[str, Any] | None = None
            for item in ready:
                item_cost = costs[item.subtask_id]
                if float(item_cost["total_cost"]) <= remaining + 1e-9:
                    selected = item
                    selected_cost = item_cost
                    break
                deferred_budget.append(item.subtask_id)
                # Stable priority order is not bypassed for a cheaper node.
                break
            if selected is None or selected_cost is None:
                break

            subtask = selected
            step = selected_cost
            job = candidate.jobs[subtask.job_id]
            injected = _anomalies_for_subtask(events, subtask.subtask_id)

            cost_units = float(step["total_cost"])
            remaining = max(0.0, remaining - cost_units)
            start_clock = round_clock
            end_clock = round_clock + float(step["duration_ms"])
            started.append(subtask.subtask_id)
            slots -= 1
            failed_event = next(
                (
                    event
                    for event in injected
                    if event.anomaly_type == "post_launch_failure"
                ),
                None,
            )
            if failed_event is not None:
                add_anomaly(
                    kind="post_launch_failure",
                    subtask=subtask,
                    source_event=failed_event,
                    message="injected failure after subtask launch",
                    persistent=False,
                )
                final_status = TERMINAL_FAILED
                reason = "post_launch_failure"
                committed = False
            elif end_clock >= job.deadline_clock_ms:
                final_status = TERMINAL_DEADLINE
                reason = "deadline_exceeded"
                committed = False
            else:
                final_status = TERMINAL_SUCCESS
                reason = "within_budget_and_deadline"
                committed = True
            status[subtask.subtask_id] = final_status
            results[subtask.subtask_id] = {
                "subtask_id": subtask.subtask_id,
                "job_id": subtask.job_id,
                "ordinal": subtask.ordinal,
                "phase": subtask.phase,
                "status": final_status,
                "reason": reason,
                "started": True,
                "start_clock_ms": start_clock,
                "end_clock_ms": end_clock,
                "deadline_clock_ms": job.deadline_clock_ms,
                "cost": step,
                "committed": committed,
                "event_id": failed_event.event_id if failed_event else None,
            }

        rounds_output.append(
            {
                "round_index": round_index,
                "start_clock_ms": round_clock,
                "budget": config.budget,
                "used_budget": round(config.budget - remaining, 6),
                "remaining_budget": round(remaining, 6),
                "slots": config.max_concurrency,
                "slots_used": len(started),
                "started_subtask_ids": started,
                "budget_deferred_subtask_ids": sorted(set(deferred_budget)),
            }
        )

    # Every node receives an explicit terminal disposition; nothing is silently
    # dropped when the round horizon ends.
    for subtask in all_subtasks:
        subtask_id = subtask.subtask_id
        if status[subtask_id] != "pending":
            continue
        dep_statuses = [status.get(dep, "missing") for dep in subtask.dependencies]
        if any(dep != TERMINAL_SUCCESS for dep in dep_statuses):
            status[subtask_id] = TERMINAL_DEPENDENCY
            results[subtask_id] = {
                "subtask_id": subtask_id,
                "job_id": subtask.job_id,
                "ordinal": subtask.ordinal,
                "phase": subtask.phase,
                "status": TERMINAL_DEPENDENCY,
                "reason": "dependency_not_committed",
                "started": False,
                "start_clock_ms": None,
                "end_clock_ms": None,
                "cost": costs[subtask_id],
            }
        else:
            status[subtask_id] = TERMINAL_ABANDONED
            results[subtask_id] = {
                "subtask_id": subtask_id,
                "job_id": subtask.job_id,
                "ordinal": subtask.ordinal,
                "phase": subtask.phase,
                "status": TERMINAL_ABANDONED,
                "reason": "round_budget_exhausted",
                "started": False,
                "start_clock_ms": None,
                "end_clock_ms": None,
                "cost": costs[subtask_id],
            }

    job_results: dict[str, list[dict[str, Any]]] = {}
    for result in results.values():
        job_results.setdefault(result["job_id"], []).append(result)
    for items in job_results.values():
        items.sort(key=lambda item: (item["ordinal"], item["subtask_id"]))

    arbitrations: list[dict[str, Any]] = []
    for job_id in sorted(candidate.jobs):
        job = candidate.jobs[job_id]
        items = job_results.get(job_id, [])
        bad_items = [
            item
            for item in items
            if item["status"]
            in (
                TERMINAL_DEADLINE,
                TERMINAL_FAILED,
                TERMINAL_BLOCKED,
                TERMINAL_ABANDONED,
                TERMINAL_DEPENDENCY,
            )
        ]
        if bad_items:
            first_bad_ordinal = min(item["ordinal"] for item in bad_items)
            overflow_chain = [
                item["subtask_id"]
                for item in items
                if item["ordinal"] >= first_bad_ordinal
            ]
            reason_counts: dict[str, int] = {}
            for item in bad_items:
                reason_counts[item["reason"]] = reason_counts.get(item["reason"], 0) + 1
            impacted = _critical_path_members(plan, candidate, job_id) | _descendant_jobs(
                candidate, [job_id]
            )
            if any(item["status"] == TERMINAL_DEADLINE for item in bad_items):
                decision_status = "deadline_overflow"
            elif any(item["status"] == TERMINAL_BLOCKED for item in bad_items):
                decision_status = "anomaly_blocked"
            elif any(item["status"] == TERMINAL_FAILED for item in bad_items):
                decision_status = "post_launch_failed"
            else:
                decision_status = "budget_abandoned"
            reason = "deadline_exceeded"
            if TERMINAL_ABANDONED in {item["status"] for item in bad_items}:
                reason = "round_budget_exhausted"
            elif TERMINAL_BLOCKED in {item["status"] for item in bad_items}:
                reason = "resource_or_deadlock_anomaly"
        else:
            decision_status = "within_deadline"
            reason = "all_subtasks_committed"
            overflow_chain = []
            impacted = {job_id}
            reason_counts = {}
        signature = digest_obj(
            {
                "plan_version": plan.plan_version,
                "job_id": job_id,
                "deadline_clock_ms": job.deadline_clock_ms,
            }
        )
        arbitrations.append(
            {
                "decision_id": "arb_" + signature,
                "signature": signature,
                "plan_version": plan.plan_version,
                "candidate_id": candidate.candidate_id,
                "job_id": job_id,
                "priority": job.priority,
                "deadline_clock_ms": job.deadline_clock_ms,
                "status": decision_status,
                "reason": reason,
                "reason_counts": dict(sorted(reason_counts.items())),
                "overflow_chain": overflow_chain,
                "affected_jobs": sorted(impacted),
                "critical_path_id": job.critical_path_id or None,
            }
        )
    arbitrations.sort(
        key=lambda item: (
            item["deadline_clock_ms"],
            item["priority"],
            item["job_id"],
            item["decision_id"],
        )
    )
    for index, decision in enumerate(arbitrations):
        decision["deadline_order_index"] = index

    items_sorted = sorted(
        results.values(),
        key=lambda item: (
            item["job_id"],
            item["ordinal"],
            item["subtask_id"],
        ),
    )
    anomaly_list = sorted(
        anomalies.values(),
        key=lambda item: (
            item["logical_clock_ms"],
            item["job_id"],
            item["subtask_id"],
            item["anomaly_id"],
        ),
    )
    run_payload = {
        "plan_version": plan.plan_version,
        "plan_hash": plan.plan_hash,
        "candidate_id": candidate.candidate_id,
        "cost_result_digest": cost_result["result_digest"],
        "config": config.to_dict(),
    }
    run_id = "run_" + digest_obj(run_payload)
    output: dict[str, Any] = {
        "run_id": run_id,
        "run_config": config.to_dict(),
        "plan_version": plan.plan_version,
        "candidate_id": candidate.candidate_id,
        "rounds": rounds_output,
        "items": items_sorted,
        "arbitrations": arbitrations,
        "anomalies": anomaly_list,
        "summary": {
            "subtasks": len(items_sorted),
            "committed": sum(1 for item in items_sorted if item["status"] == TERMINAL_SUCCESS),
            "deadline_miss": sum(
                1 for item in items_sorted if item["status"] == TERMINAL_DEADLINE
            ),
            "failed_after_start": sum(
                1 for item in items_sorted if item["status"] == TERMINAL_FAILED
            ),
            "blocked_anomaly": sum(
                1 for item in items_sorted if item["status"] == TERMINAL_BLOCKED
            ),
            "blocked_dependency": sum(
                1 for item in items_sorted if item["status"] == TERMINAL_DEPENDENCY
            ),
            "abandoned_budget": sum(
                1 for item in items_sorted if item["status"] == TERMINAL_ABANDONED
            ),
            "total_budget_used": round(
                sum(float(item["cost"]["total_cost"]) for item in items_sorted if item["started"]),
                6,
            ),
            "anomalies": len(anomaly_list),
        },
    }
    output["result_digest"] = digest_obj(output)
    return output

