"""Deterministic three-constraint scheduler and degradation policy."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from .errors import (
    E_COMPUTE_BUDGET,
    E_MEMORY_BUDGET,
    E_SLOT_BUDGET,
)
from .models import PipelineConfig, Task, manifest_checksum
from .resources import ResourceEstimate, estimate_task


PLANNER_POLICY = {
    "candidate_order": [
        "priority_descending",
        "shared_infer_weight_benefit_descending",
        "parameter_count_ascending",
        "job_id_ascending",
    ],
    "degradation_order": [
        "shrink_batch_size_by_half_while_preserving_iterations",
        "then_shrink_iterations_by_half",
        "stop_at_min_batch_size_and_min_iterations",
    ],
    "degradation_fields": {
        "train": ["batch_size", "steps"],
        "infer": ["batch_size", "warmup"],
    },
    "rejection_code_precedence": [
        E_SLOT_BUDGET,
        E_COMPUTE_BUDGET,
        E_MEMORY_BUDGET,
    ],
    "weight_sharing": (
        "infer jobs with the same model_tag share one resident read-only "
        "weight allocation; train jobs always reserve a private weight copy"
    ),
}


def _halving_values(start: int, minimum: int) -> list[int]:
    values: list[int] = []
    current = start
    while True:
        if current not in values:
            values.append(current)
        if current <= minimum:
            break
        next_value = max(minimum, current // 2)
        if next_value == current:
            break
        current = next_value
    return values


def _degradation_candidates(
    task: Task,
    config: PipelineConfig,
) -> list[tuple[int, int]]:
    batch_values = _halving_values(task.batch_size, config.min_batch_size)
    iteration_values = _halving_values(task.iterations, config.min_iterations)
    candidates: list[tuple[int, int]] = []
    # Preserve iterations first while reducing activation memory/compute, then
    # progressively reduce iterations. This order is part of the public policy.
    for iterations in iteration_values:
        for batch_size in batch_values:
            pair = (batch_size, iterations)
            if pair == (task.batch_size, task.iterations):
                continue
            if pair not in candidates:
                candidates.append(pair)
            if len(candidates) >= config.max_degrade_candidates:
                return candidates
    return candidates


def _attempt_record(
    task: Task,
    batch_size: int,
    iterations: int,
    estimate: ResourceEstimate,
    remaining_compute: int,
    remaining_memory: int,
    slot_limit: int,
) -> dict[str, Any]:
    return {
        "batch_size": batch_size,
        task.iteration_field: iterations,
        "fits_slots": slot_limit >= estimate.slot_units,
        "fits_compute": estimate.compute_units <= remaining_compute,
        "fits_memory": estimate.memory_bytes <= remaining_memory,
        "compute_units": estimate.compute_units,
        "memory_bytes": estimate.memory_bytes,
        "memory_mb": estimate.memory_mb,
        "weight_charged_bytes": estimate.weight_charged_bytes,
        "weight_reused": estimate.weight_reused,
    }


def _pick_rejection_code(
    full_estimate: ResourceEstimate,
    remaining_compute: int,
    remaining_memory: int,
    config: PipelineConfig,
) -> tuple[str, list[str]]:
    constraints: list[str] = []
    if full_estimate.compute_units > remaining_compute:
        constraints.append(E_COMPUTE_BUDGET)
    if full_estimate.memory_bytes > remaining_memory:
        constraints.append(E_MEMORY_BUDGET)
    if config.max_slots < full_estimate.slot_units:
        constraints.append(E_SLOT_BUDGET)
    for code in config.rejection_code_precedence:
        if code in constraints:
            return code, constraints
    # This fallback is reachable only if a malformed policy hides all matched
    # constraints; report the most resource-conservative code deterministically.
    return (constraints[0] if constraints else E_COMPUTE_BUDGET), constraints


def build_plan(tasks: Iterable[Task], config: PipelineConfig) -> dict[str, Any]:
    config.validate()
    task_list = list(tasks)
    infer_tag_counts = Counter(
        task.model_tag for task in task_list if task.job_kind == "infer"
    )

    def shared_benefit(task: Task) -> int:
        if task.job_kind != "infer":
            return 0
        extra_sharers = max(0, infer_tag_counts[task.model_tag] - 1)
        return extra_sharers * (task.parameter_count * config.bytes_per_scalar)

    ordered_tasks = sorted(
        task_list,
        key=lambda task: (
            -task.priority,
            -shared_benefit(task),
            task.parameter_count,
            task.job_id,
        ),
    )

    used_compute = 0
    used_memory = 0
    resident_infer_tags: set[str] = set()
    decisions: list[dict[str, Any]] = []

    for task in ordered_tasks:
        remaining_compute = max(0, config.compute_budget - used_compute)
        remaining_memory = max(0, config.memory_budget_bytes - used_memory)
        shared_weight = (
            task.job_kind == "infer" and task.model_tag in resident_infer_tags
        )
        full_estimate = estimate_task(
            task,
            config,
            infer_weight_reused=shared_weight,
        )
        full_fits = (
            config.max_slots >= full_estimate.slot_units
            and full_estimate.compute_units <= remaining_compute
            and full_estimate.memory_bytes <= remaining_memory
        )
        attempts = [
            _attempt_record(
                task,
                task.batch_size,
                task.iterations,
                full_estimate,
                remaining_compute,
                remaining_memory,
                config.max_slots,
            )
        ]

        selected: tuple[int, int, ResourceEstimate] | None = None
        if full_fits:
            selected = (task.batch_size, task.iterations, full_estimate)
        elif (
            config.degradation_enabled
            and task.priority >= config.degradation_priority_floor
        ):
            for batch_size, iterations in _degradation_candidates(task, config):
                candidate = task.with_effective(batch_size, iterations)
                candidate_estimate = estimate_task(
                    candidate,
                    config,
                    infer_weight_reused=shared_weight,
                )
                attempts.append(
                    _attempt_record(
                        task,
                        batch_size,
                        iterations,
                        candidate_estimate,
                        remaining_compute,
                        remaining_memory,
                        config.max_slots,
                    )
                )
                if (
                    config.max_slots >= candidate_estimate.slot_units
                    and candidate_estimate.compute_units <= remaining_compute
                    and candidate_estimate.memory_bytes <= remaining_memory
                ):
                    selected = (batch_size, iterations, candidate_estimate)
                    break

        base_decision: dict[str, Any] = {
            "job_id": task.job_id,
            "job_kind": task.job_kind,
            "model_tag": task.model_tag,
            "priority": task.priority,
            "original_task": task.to_dict(),
            "order_basis": {
                "priority": task.priority,
                "shared_infer_weight_benefit_bytes": shared_benefit(task),
                "parameter_count": task.parameter_count,
                "job_id": task.job_id,
            },
            "remaining_before": {
                "compute_units": remaining_compute,
                "memory_bytes": remaining_memory,
                "slots": config.max_slots,
            },
            "original_estimate": full_estimate.to_dict(),
            "shared_weight_reused": shared_weight,
            "attempted_estimates": attempts,
        }

        if selected is not None:
            batch_size, iterations, effective_estimate = selected
            effective_task = task.with_effective(batch_size, iterations)
            degraded = effective_task != task
            degraded_fields: dict[str, dict[str, int]] = {}
            if batch_size != task.batch_size:
                degraded_fields["batch_size"] = {
                    "from": task.batch_size,
                    "to": batch_size,
                }
            if iterations != task.iterations:
                degraded_fields[task.iteration_field] = {
                    "from": task.iterations,
                    "to": iterations,
                }
            if degraded_fields and "batch_size" in degraded_fields and len(degraded_fields) > 1:
                degrade_category = "batch_and_iterations"
            elif "batch_size" in degraded_fields:
                degrade_category = "batch_size"
            elif degraded_fields:
                degrade_category = "iterations"
            else:
                degrade_category = None

            used_compute += effective_estimate.compute_units
            used_memory += effective_estimate.memory_bytes
            if task.job_kind == "infer":
                resident_infer_tags.add(task.model_tag)

            base_decision.update(
                {
                    "decision": "admitted",
                    "effective_task": effective_task.to_dict(),
                    "effective_estimate": effective_estimate.to_dict(),
                    "degraded": degraded,
                    "degrade_category": degrade_category,
                    "degraded_fields": degraded_fields,
                    "degrade_returned": {
                        "compute_units": max(
                            0,
                            full_estimate.compute_units
                            - effective_estimate.compute_units,
                        ),
                        "memory_bytes": max(
                            0,
                            full_estimate.memory_bytes
                            - effective_estimate.memory_bytes,
                        ),
                    },
                    "reason": (
                        "admitted_at_full_request"
                        if not degraded
                        else "admitted_with_first_fitting_degradation"
                    ),
                }
            )
        else:
            code, constraints = _pick_rejection_code(
                full_estimate,
                remaining_compute,
                remaining_memory,
                config,
            )
            if config.max_slots < full_estimate.slot_units:
                reason = "no_parallel_slot_can_host_job"
            elif code == E_COMPUTE_BUDGET:
                reason = "remaining_compute_budget_cannot_fit_full_or_degraded_job"
            elif code == E_MEMORY_BUDGET:
                reason = "remaining_resident_memory_cannot_fit_full_or_degraded_job"
            else:
                reason = "scheduler_has_no_capacity"
            base_decision.update(
                {
                    "decision": "rejected",
                    "effective_task": None,
                    "effective_estimate": None,
                    "degraded": False,
                    "degrade_category": None,
                    "degraded_fields": {},
                    "business_code": code,
                    "stage": "schedule",
                    "reason": reason,
                    "blocking_constraints": constraints,
                    "shortfall": {
                        "compute_units": max(
                            0, full_estimate.compute_units - remaining_compute
                        ),
                        "memory_bytes": max(
                            0, full_estimate.memory_bytes - remaining_memory
                        ),
                    },
                }
            )
        decisions.append(base_decision)

    admitted = [
        decision for decision in decisions if decision["decision"] == "admitted"
    ]
    rejected = [
        decision for decision in decisions if decision["decision"] == "rejected"
    ]
    degraded = [decision for decision in admitted if decision["degraded"]]
    execution_order = [decision["job_id"] for decision in admitted]
    groups: list[dict[str, Any]] = []
    if admitted:
        for group_index, start in enumerate(
            range(0, len(admitted), config.max_slots)
        ):
            members = admitted[start : start + config.max_slots]
            groups.append(
                {
                    "group_index": group_index,
                    "job_ids": [member["job_id"] for member in members],
                    "slot_count": sum(
                        member["effective_estimate"]["slot_units"]
                        for member in members
                    ),
                    "estimated_compute_units": sum(
                        member["effective_estimate"]["compute_units"]
                        for member in members
                    ),
                    "estimated_memory_bytes": sum(
                        member["effective_estimate"]["memory_bytes"]
                        for member in members
                    ),
                }
            )

    rejection_counts = Counter(
        decision["business_code"] for decision in rejected
    )
    plan = {
        "schema_version": 1,
        "planner_policy": PLANNER_POLICY,
        "config": config.to_dict(),
        "manifest_checksum": manifest_checksum(task_list),
        "candidate_count": len(task_list),
        "decisions": decisions,
        "admitted_job_ids": [decision["job_id"] for decision in admitted],
        "rejected_job_ids": [decision["job_id"] for decision in rejected],
        "degraded_job_ids": [decision["job_id"] for decision in degraded],
        "execution_order": execution_order,
        "parallel_groups": groups,
        "totals": {
            "admitted": len(admitted),
            "rejected": len(rejected),
            "degraded": len(degraded),
            "compute_units_used": sum(
                decision["effective_estimate"]["compute_units"]
                for decision in admitted
            ),
            "compute_units_limit": config.compute_budget,
            "memory_bytes_used": sum(
                decision["effective_estimate"]["memory_bytes"]
                for decision in admitted
            ),
            "memory_bytes_limit": config.memory_budget_bytes,
            "slot_limit": config.max_slots,
            "slot_peak": max(
                (group["slot_count"] for group in groups),
                default=0,
            ),
            "parallel_group_count": len(groups),
        },
        "rejection_counts": dict(sorted(rejection_counts.items())),
    }
    return plan
