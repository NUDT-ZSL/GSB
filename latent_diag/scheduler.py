from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import EngineError
from .mathops import make_ops
from .task import TaskSpec, compare_task_fields, validate_manifest
from .config import EffectiveConfig


@dataclass
class InvalidationPlan:
    affected: list[str] = field(default_factory=list)
    reuse_candidates: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    reasons: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "affected": list(self.affected),
            "reuse_candidates": list(self.reuse_candidates),
            "unchanged": list(self.unchanged),
            "removed": list(self.removed),
            "reasons": {key: list(value) for key, value in self.reasons.items()},
            "recompute_reuse_disjoint": set(self.affected).isdisjoint(
                self.reuse_candidates
            ),
            "recompute_reuse_union": sorted(
                set(self.affected) | set(self.reuse_candidates)
            ),
        }


def plan_invalidations(
    previous_tasks: Iterable[TaskSpec],
    current_tasks: Iterable[TaskSpec],
) -> InvalidationPlan:
    old_by_id = {task.task_id: task for task in previous_tasks}
    new_by_id = {task.task_id: task for task in current_tasks}
    plan = InvalidationPlan()
    direct_dirty: set[str] = set()
    propagating: set[str] = set()

    for task in new_by_id.values():
        old = old_by_id.get(task.task_id)
        if old is None:
            direct_dirty.add(task.task_id)
            plan.reasons[task.task_id] = ["new_task"]
            continue
        if task.revision < old.revision:
            raise EngineError(
                "E_REVISION_REGRESSION",
                "task revision must be monotonic",
                task_id=task.task_id,
                details={"old_revision": old.revision, "new_revision": task.revision},
            )
        changed = compare_task_fields(old, task)
        if changed:
            direct_dirty.add(task.task_id)
            plan.reasons[task.task_id] = [f"changed:{name}" for name in sorted(changed)]
            propagating.add(task.task_id)
        elif task.revision > old.revision:
            direct_dirty.add(task.task_id)
            plan.reasons.setdefault(task.task_id, []).append("revision_bump_only")
        else:
            plan.unchanged.append(task.task_id)

    for task_id in sorted(set(old_by_id) - set(new_by_id)):
        plan.removed.append(task_id)
        propagating.add(task_id)

    dependents: dict[str, set[str]] = {task_id: set() for task_id in new_by_id}
    for task in new_by_id.values():
        for dependency in task.depends_on:
            if dependency in dependents:
                dependents[dependency].add(task.task_id)
    queue = list(propagating)
    downstream: set[str] = set()
    while queue:
        parent = queue.pop(0)
        for child in sorted(dependents.get(parent, set())):
            if child in downstream:
                continue
            downstream.add(child)
            queue.append(child)
    for task_id in sorted(downstream):
        direct_dirty.add(task_id)
        plan.reasons.setdefault(task_id, []).append("upstream_invalidated")

    plan.affected = [task.task_id for task in current_tasks if task.task_id in direct_dirty]
    plan.reuse_candidates = [
        task.task_id for task in current_tasks if task.task_id not in direct_dirty
    ]
    plan.unchanged = [
        task_id for task_id in plan.unchanged if task_id not in direct_dirty
    ]
    return plan


def estimate_task_memory(task: TaskSpec) -> int:
    elements = task.channels * task.height * task.width
    ops = make_ops(task.dtype_profile, task.quant_scale, task.rounding_mode)
    steps = task.sampler_steps
    # The implementation retains candidate, same-profile-full and float64-reference
    # step arrays, plus final candidate/reference/full-frame copies and RGB buffers.
    step_and_final_latents = elements * (
        ops.itemsize * (steps * 2 + 3) + 8 * (steps + 3)
    )
    rgb_buffers = task.height * task.width * 3 * (8 + 1)
    scalar_overhead = 32 * 1024
    return int(step_and_final_latents + rgb_buffers + scalar_overhead)


@dataclass
class AdmissionResult:
    accepted: list[str]
    rejected: dict[str, dict[str, Any]]
    batches: list[list[str]]
    total_iterations: int
    batch_peak_bytes: list[int]

    @property
    def peak_memory_bytes(self) -> int:
        return max(self.batch_peak_bytes, default=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": list(self.accepted),
            "rejected": dict(self.rejected),
            "batches": [list(batch) for batch in self.batches],
            "total_denoise_iterations": self.total_iterations,
            "peak_resident_latent_bytes": self.peak_memory_bytes,
            "batch_peak_bytes": list(self.batch_peak_bytes),
        }


def admit_tasks(tasks: list[TaskSpec], config: EffectiveConfig) -> AdmissionResult:
    validate_manifest(tasks, config)
    budget = config.budget
    iteration_limit = int(budget["max_total_denoise_iterations"])
    memory_limit = int(budget["max_resident_latent_bytes"])
    batch_size = int(budget["tasks_per_batch"])
    priority = list(budget["priority"])

    accepted: list[str] = []
    rejected: dict[str, dict[str, Any]] = {}
    batches: list[list[str]] = []
    total_iterations = 0
    batch_peak_bytes: list[int] = []

    for batch_start in range(0, len(tasks), batch_size):
        batch_tasks = tasks[batch_start : batch_start + batch_size]
        current_batch: list[str] = []
        batch_memory = 0
        for task in batch_tasks:
            iteration_cost = task.denoise_iterations
            memory_cost = estimate_task_memory(task)
            checks = {
                "iterations": (total_iterations + iteration_cost > iteration_limit),
                "memory": (batch_memory + memory_cost > memory_limit),
            }
            error_code = None
            chosen = None
            for dimension in priority:
                if checks[dimension]:
                    chosen = dimension
                    error_code = (
                        "E_ITER_BUDGET" if dimension == "iterations" else "E_MEMORY_BUDGET"
                    )
                    break
            if chosen is not None:
                rejected[task.task_id] = {
                    "code": error_code,
                    "reason": chosen,
                    "task_id": task.task_id,
                    "iteration_cost": iteration_cost,
                    "projected_iterations": total_iterations + iteration_cost,
                    "iteration_limit": iteration_limit,
                    "memory_cost": memory_cost,
                    "projected_batch_memory": batch_memory + memory_cost,
                    "memory_limit": memory_limit,
                    "budget_priority": priority,
                }
                continue
            accepted.append(task.task_id)
            current_batch.append(task.task_id)
            total_iterations += iteration_cost
            batch_memory += memory_cost
        if current_batch:
            batches.append(current_batch)
        batch_peak_bytes.append(batch_memory)

    return AdmissionResult(
        accepted=accepted,
        rejected=rejected,
        batches=batches,
        total_iterations=total_iterations,
        batch_peak_bytes=batch_peak_bytes,
    )

