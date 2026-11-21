"""Pair-chunked parallel scheduling with explicit resource arbitration.

The arbiter is a *pure function* of a resource observation and the budget, so
the same observation always produces the same decision and its rationale.
Arbitration follows the configured priority order across the three competing
concerns: progress (finishing the stage), memory (resident embeddings) and
parallelism (worker/task slots).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from .config import BudgetConfig, SchedulingDecision
from .errors import ResourceLimitError

DEGRADE_BATCH = "batch_size_reduced"
DEGRADE_WORKERS = "worker_count_reduced"
DEGRADE_TASKS = "parallel_tasks_reduced"


@dataclass(frozen=True)
class ResourceObservation:
    stage: str
    pair_count: int
    requested_workers: int
    requested_batch: int


def _resident(pair_count: int, budget: BudgetConfig) -> int:
    # Each pair retains one float32 embedding per stage on the driver during merge.
    return pair_count * budget.bytes_per_embedding


def _parallel_tasks(workers: int, pair_count: int, batch: int) -> int:
    if pair_count == 0:
        return 0
    chunks = (pair_count + batch - 1) // batch
    return min(workers, chunks)


def arbitrate(
    observation: ResourceObservation, budget: BudgetConfig
) -> SchedulingDecision:
    """Decide worker/batch sizes or reject with E_RESOURCE_LIMIT.

    Priority semantics (applied in declared order):
      progress    -> pair count must fit the stage cap; keep batches large
                     enough to make forward progress (>= 1 pair per task).
      memory      -> resident embeddings must fit the memory cap; shrink batch
                     (then workers) until it does.
      parallelism -> tasks must fit the slot cap; reduce workers last.
    """
    budget.validate()
    stage = observation.stage
    pair_count = observation.pair_count
    rationale: List[str] = []
    sources: List[str] = []

    # Hard admission: the stage itself must fit the per-stage pair cap.
    if pair_count > budget.max_stage_pairs:
        raise ResourceLimitError(
            f"stage {stage!r} has {pair_count} pairs, exceeding "
            f"max_stage_pairs={budget.max_stage_pairs}",
            stage=stage,
        )

    resident = _resident(pair_count, budget)
    if resident > budget.max_resident_embeddings:
        if budget.overflow_policy == "reject":
            raise ResourceLimitError(
                f"stage {stage!r} needs {resident} resident embedding bytes, exceeding "
                f"max_resident_embeddings={budget.max_resident_embeddings}",
                stage=stage,
            )
        # Resident memory is driven by total pair count, not batch; under degrade
        # policy this means the stage cannot run at all in one pass.
        raise ResourceLimitError(
            f"stage {stage!r} needs {resident} resident bytes but cap is "
            f"{budget.max_resident_embeddings}; split the stage into smaller runs",
            stage=stage,
        )

    workers = max(1, min(observation.requested_workers, budget.max_workers))
    if workers != observation.requested_workers:
        sources.append(DEGRADE_WORKERS)
        rationale.append(
            f"workers reduced to max_workers={budget.max_workers} "
            f"(requested {observation.requested_workers})"
        )

    batch = max(1, min(observation.requested_batch, budget.max_batch_pairs, pair_count))
    if batch != observation.requested_batch:
        if batch < pair_count:
            # Only hitting a configured cap counts as degradation; clamping to
            # the pair count is a natural fit, not a resource compromise.
            sources.append(DEGRADE_BATCH)
            rationale.append(
                f"batch reduced to max_batch_pairs={budget.max_batch_pairs} "
                f"(requested {observation.requested_batch})"
            )
        else:
            rationale.append(
                f"batch naturally sized to pair_count={pair_count} (no degradation)"
            )

    # Priority-driven adjustments.
    for priority in budget.arbitration_priority:
        if priority == "progress":
            # Ensure at least one pair can be dispatched per task.
            if batch < 1:
                batch = 1
                sources.append(DEGRADE_BATCH)
                rationale.append("progress priority restored minimum batch of 1")
        elif priority == "memory":
            # A task's working set scales with its batch; shrink batch first.
            while (
                batch * budget.bytes_per_embedding * max(1, workers)
                > budget.max_resident_embeddings
                and batch > 1
            ):
                batch -= 1
                if DEGRADE_BATCH not in sources:
                    sources.append(DEGRADE_BATCH)
                rationale.append(
                    "memory priority reduced batch to keep in-flight embeddings under cap"
                )
        elif priority == "parallelism":
            tasks = _parallel_tasks(workers, pair_count, batch)
            if tasks > budget.max_parallel_tasks and budget.overflow_policy == "reject":
                raise ResourceLimitError(
                    f"stage {stage!r} needs {tasks} parallel tasks, exceeding "
                    f"max_parallel_tasks={budget.max_parallel_tasks}",
                    stage=stage,
                )
            while tasks > budget.max_parallel_tasks and workers > 1:
                workers -= 1
                if DEGRADE_WORKERS not in sources:
                    sources.append(DEGRADE_WORKERS)
                rationale.append(
                    f"parallelism priority reduced workers to respect "
                    f"max_parallel_tasks={budget.max_parallel_tasks}"
                )
                tasks = _parallel_tasks(workers, pair_count, batch)

    tasks = _parallel_tasks(workers, pair_count, batch)
    if tasks > budget.max_parallel_tasks:
        sources.append(DEGRADE_TASKS)
        rationale.append(
            f"parallel task count {tasks} reported against cap "
            f"{budget.max_parallel_tasks} (chunk-bound minimum retained)"
        )

    return SchedulingDecision(
        stage=stage,
        pair_count=pair_count,
        requested_workers=observation.requested_workers,
        requested_batch=observation.requested_batch,
        chosen_workers=workers,
        chosen_batch=batch,
        resident_embeddings=resident,
        parallel_tasks=tasks,
        degraded=bool(sources),
        degradation_sources=tuple(sources),
        rationale=tuple(rationale),
    )


def chunk_pairs(pair_ids: Sequence[str], batch: int) -> List[Tuple[str, ...]]:
    """Deterministic fixed-size chunks over an already canonically ordered list."""
    if batch <= 0:
        raise ValueError("batch must be positive")
    return [tuple(pair_ids[i : i + batch]) for i in range(0, len(pair_ids), batch)]


def env_flag(name: str) -> bool:
    return os.environ.get(name, "") in ("1", "true", "TRUE", "yes")

