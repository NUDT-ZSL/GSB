"""Parallel scheduler with coupled resource budgets (requirements 3 and 4).

Design notes:
  * Tasks are independent per-target groups; results are merged and sorted
    by stable keys, so completion order never changes the conclusion.
  * Concurrency, per-task timeout, queue capacity, per-task memory limit
    and the cumulative batch memory budget are all configurable.
  * Budget decisions use measured values only: wall-clock elapsed time
    (time.perf_counter) and actual traced allocation (tracemalloc delta).
    Declared hints in task payloads merely drive real work (real
    allocation, real CPU spin); they are never trusted as measurements.
  * A task that exceeds its limits is marked with a resource business
    code and the rest of the batch continues. When the cumulative budget
    is exhausted the batch stops scheduling and reports a batch-level
    conclusion (E3004).
  * When the queue is saturated, enqueue attempts are rejected with
    E3001 and recorded - never silently dropped.
"""

from __future__ import annotations

import time
import tracemalloc
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from . import errors
from .detector import detect_group
from .models import NormalizedRule, TaskOutcome, TaskSpec


@dataclass(frozen=True)
class SchedulerConfig:
    concurrency: int = 2
    task_timeout_sec: float = 5.0
    queue_limit: int = 64
    task_memory_limit_bytes: int = 64 * 1024 * 1024
    batch_memory_budget_bytes: int = 512 * 1024 * 1024
    # Grace period (seconds) spent waiting for a queue slot before the
    # enqueue attempt is rejected with E3001.
    enqueue_grace_sec: float = 0.05


@dataclass
class BatchResult:
    outcomes: list[TaskOutcome]
    events: list[dict[str, str]]
    conclusion: str  # "completed" | "budget_exhausted"
    budget_consumed_bytes: int


def plan_tasks(rules: Iterable[NormalizedRule]) -> list[TaskSpec]:
    """Split validation work into independent per-target tasks."""
    groups: dict[str, list[str]] = {}
    for rule in rules:
        groups.setdefault(rule.target, []).append(rule.rule_id)
    return [
        TaskSpec(task_id=f"group:{target}", target=target,
                 rule_ids=tuple(sorted(ids)))
        for target, ids in sorted(groups.items())
    ]


def _busy_spin(seconds: float) -> None:
    deadline = time.perf_counter() + seconds
    acc = 0
    while time.perf_counter() < deadline:
        acc += 1
    assert acc > 0


def execute_task(task: TaskSpec, rules_by_id: dict[str, NormalizedRule],
                 config: SchedulerConfig) -> TaskOutcome:
    """Run one task in a worker thread, measuring real time and memory."""
    if not tracemalloc.is_tracing():
        tracemalloc.start()
    mem_before = tracemalloc.get_traced_memory()[0]
    start = time.perf_counter()
    error = None
    findings = None
    buffer = None
    try:
        sim = task.simulate or {}
        alloc = int(sim.get("alloc_bytes", 0))
        if alloc > 0:
            buffer = bytearray(alloc)  # real allocation, really measured
            buffer[0::4097] = b"\x01" * len(buffer[0::4097])  # touch pages
        spin_ms = float(sim.get("spin_ms", 0))
        if spin_ms > 0:
            _busy_spin(spin_ms / 1000.0)  # real CPU time, really measured
        group_rules = [rules_by_id[rid] for rid in sorted(task.rule_ids)
                       if rid in rules_by_id]
        findings = detect_group(group_rules)
    except Exception as exc:  # task isolation boundary
        error = f"{type(exc).__name__}: {exc}"
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    mem_bytes = max(0, tracemalloc.get_traced_memory()[0] - mem_before)
    del buffer  # release only after the measurement is taken

    if error is not None:
        return TaskOutcome(task.task_id, "failed", None, elapsed_ms, mem_bytes,
                           None, error)
    if mem_bytes > config.task_memory_limit_bytes:
        return TaskOutcome(task.task_id, "memory_exceeded",
                           errors.E_TASK_MEMORY_EXCEEDED, elapsed_ms, mem_bytes,
                           None)
    if elapsed_ms > config.task_timeout_sec * 1000.0:
        return TaskOutcome(task.task_id, "timeout", errors.E_TASK_TIMEOUT,
                           elapsed_ms, mem_bytes, None)
    return TaskOutcome(task.task_id, "succeeded", None, elapsed_ms, mem_bytes,
                       findings)


class SimulatedCrash(BaseException):
    """Test hook: mimics an abrupt kill (no cleanup handlers run)."""


def run_batch(tasks: Iterable[TaskSpec],
              rules_by_id: dict[str, NormalizedRule],
              config: SchedulerConfig,
              resumed_state: dict[str, Any] | None = None,
              checkpoint_save: Callable[[dict[str, Any]], None] | None = None,
              kill_hook: Callable[[int], None] | None = None) -> BatchResult:
    """Execute a batch of tasks, optionally resuming from a checkpoint.

    ``checkpoint_save`` is invoked with the full checkpoint state after
    every collected task. ``kill_hook`` (tests only) runs right after each
    checkpoint write and may raise to simulate a forced termination.
    """
    if not tracemalloc.is_tracing():
        tracemalloc.start()

    completed: dict[str, TaskOutcome] = {}
    budget_consumed = 0
    if resumed_state:
        for task_id, data in resumed_state.get("completed", {}).items():
            completed[task_id] = TaskOutcome.from_dict(data)
        budget_consumed = int(resumed_state.get("budget_consumed", {}).get("mem_bytes", 0))

    events: list[dict[str, str]] = []
    pending = sorted((t for t in tasks if t.task_id not in completed),
                     key=lambda t: t.task_id)

    outcomes: list[TaskOutcome] = list(completed.values())
    budget_exhausted = budget_consumed >= config.batch_memory_budget_bytes
    if budget_exhausted and pending:
        events.append({"code": errors.E_BATCH_BUDGET_EXHAUSTED,
                       "message": "cumulative memory budget already exhausted at resume"})

    def persist() -> None:
        if checkpoint_save is None:
            return
        state = {
            "completed": {tid: o.to_dict() for tid, o in sorted(completed.items())},
            "budget_consumed": {"mem_bytes": budget_consumed},
        }
        checkpoint_save(state)
        if kill_hook is not None:
            kill_hook(len(completed))

    def collect(outcome: TaskOutcome) -> None:
        nonlocal budget_consumed, budget_exhausted
        completed[outcome.task_id] = outcome
        outcomes.append(outcome)
        budget_consumed += outcome.mem_bytes
        if not budget_exhausted and budget_consumed >= config.batch_memory_budget_bytes:
            budget_exhausted = True
            events.append({"code": errors.E_BATCH_BUDGET_EXHAUSTED,
                           "message": "cumulative memory budget exhausted; "
                                      "remaining tasks will not be scheduled"})
        persist()

    with ThreadPoolExecutor(max_workers=max(1, config.concurrency)) as pool:
        inflight: dict[Any, TaskSpec] = {}

        def drain(done_futures) -> None:
            for future in done_futures:
                inflight.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:  # defensive; execute_task already isolates
                    outcome = TaskOutcome("unknown", "failed", None, 0.0, 0,
                                          None, f"{type(exc).__name__}: {exc}")
                collect(outcome)

        for task in pending:
            if budget_exhausted:
                collect(TaskOutcome(task.task_id, "not_run",
                                    errors.E_BATCH_BUDGET_EXHAUSTED, 0.0, 0,
                                    None))
                continue
            # Enqueue with bounded wait; reject (never silently drop) when
            # the queue stays saturated beyond the grace period.
            if len(inflight) >= config.queue_limit:
                deadline = time.perf_counter() + config.enqueue_grace_sec
                while len(inflight) >= config.queue_limit:
                    done, _ = wait(list(inflight), timeout=0.005,
                                   return_when=FIRST_COMPLETED)
                    if done:
                        drain(done)
                    elif time.perf_counter() >= deadline:
                        break
            # The wait above may have exhausted the budget; re-check before
            # committing a new task to the queue.
            if budget_exhausted:
                collect(TaskOutcome(task.task_id, "not_run",
                                    errors.E_BATCH_BUDGET_EXHAUSTED, 0.0, 0,
                                    None))
                continue
            if len(inflight) >= config.queue_limit:
                collect(TaskOutcome(task.task_id, "rejected_queue",
                                    errors.E_QUEUE_FULL, 0.0, 0, None))
                continue
            future = pool.submit(execute_task, task, rules_by_id, config)
            inflight[future] = task

        while inflight:
            done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
            drain(done)

    conclusion = "budget_exhausted" if budget_exhausted else "completed"
    outcomes.sort(key=lambda o: o.task_id)
    return BatchResult(outcomes=outcomes, events=events,
                       conclusion=conclusion,
                       budget_consumed_bytes=budget_consumed)
