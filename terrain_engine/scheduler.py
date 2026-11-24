"""Deterministic parallel scheduler with dual-budget admission control.

Two budgets gate every tile:

* a per-round *logical time* budget metered by an injectable clock;
* an *in-flight sample-buffer memory* budget measured in bytes.

Admission is decided by a single-threaded, deterministic discrete-event
simulation over tile order, declared buffer sizes and declared logical
costs.  Real rendering then runs in a bounded thread pool whose concurrency
and memory semaphore mirror the simulation.  Because the planner never
consults wall-clock time or thread scheduling, rounds and admission order
are reproducible for any ``max_concurrency`` value.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import errors
from .model import TaskSpec, TileSpec
from .render import sample_buffer_bytes


GRACEFUL_EXIT_CODE = 130


class _MemoryLimiter:
    """Block until ``size`` bytes of in-flight budget are available."""

    def __init__(self, budget_bytes: int) -> None:
        self._capacity = budget_bytes
        self._inflight = 0
        self._condition = threading.Condition()

    def acquire(self, size: int) -> None:
        with self._condition:
            while self._inflight + size > self._capacity:
                self._condition.wait()
            self._inflight += size

    def release(self, size: int) -> None:
        with self._condition:
            self._inflight -= size
            if self._inflight < 0:  # pragma: no cover - defensive guard
                raise RuntimeError("in-flight memory accounting underflow")
            self._condition.notify_all()


@dataclass
class TileResult:
    tile_id: str
    ok: bool
    round_index: int
    logical_cost_ms: float
    buffer_bytes: int
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class RoundPlan:
    round_index: int
    admitted: List[str] = field(default_factory=list)
    deferred: List[str] = field(default_factory=list)
    logical_elapsed_ms: float = 0.0
    peak_inflight_bytes: int = 0


@dataclass
class ScheduleReport:
    rounds: List[RoundPlan] = field(default_factory=list)
    results: List[TileResult] = field(default_factory=list)

    def admitted_sequence(self) -> List[str]:
        sequence: List[str] = []
        for plan in self.rounds:
            sequence.extend(plan.admitted)
        return sequence


def default_logical_cost_ms(tile: TileSpec, samples: int) -> float:
    """Deterministic declared cost: one logical ms per 256 sub-samples."""
    sub_samples = tile.width * tile.height * samples
    return max(1.0, sub_samples / 256.0)


class LogicalClock:
    """Injectable monotonic logical clock (milliseconds)."""

    def __init__(self, cost_overrides: Optional[Dict[str, float]] = None):
        self._now = 0.0
        self._overrides = dict(cost_overrides or {})

    def now_ms(self) -> float:
        return self._now

    def charge(self, tile: TileSpec, samples: int) -> float:
        cost = self._overrides.get(
            tile.tile_id, default_logical_cost_ms(tile, samples))
        if cost <= 0:
            raise errors.fail(errors.E_BUDGET,
                              "logical tile cost must be positive",
                              tile=tile.tile_id, cost=cost)
        self._now += cost
        return cost


def plan_rounds(task: TaskSpec, pending: List[TileSpec],
                clock: LogicalClock) -> List[RoundPlan]:
    """Discrete-event simulation producing deterministic round plans.

    Rounds are driven solely by the logical-time budget and the in-flight
    memory budget; ``max_concurrency`` only bounds the real worker pool, so
    the round partition and admission sequence are identical for any
    configured concurrency.
    """
    plans: List[RoundPlan] = []
    queue = list(pending)
    round_index = 0
    while queue:
        plan = RoundPlan(round_index=round_index)
        elapsed = 0.0
        inflight = 0
        remaining = list(queue)
        blocked_on_memory: Optional[TileSpec] = None

        while remaining:
            tile = remaining[0]
            cost = clock.charge(tile, task.samples)
            buffer_bytes = sample_buffer_bytes(tile, task.samples)
            if buffer_bytes > task.memory_budget_bytes:
                raise errors.fail(
                    errors.E_BUDGET,
                    "single tile sample buffer exceeds memory budget",
                    tile=tile.tile_id, buffer_bytes=buffer_bytes,
                    budget=task.memory_budget_bytes)

            if elapsed + cost > task.time_budget_ms + 1e-12:
                # Round is over: this tile and every later tile are yielded.
                plan.deferred.extend(item.tile_id for item in remaining)
                remaining = []
                break

            if inflight + buffer_bytes > \
                    task.memory_budget_bytes:
                # Blocking head-of-line: a later smaller tile cannot jump the
                # deterministic order, so the tile waits for a future round.
                blocked_on_memory = tile
                plan.deferred.extend(item.tile_id for item in remaining)
                remaining = []
                break

            remaining.pop(0)
            inflight += buffer_bytes
            elapsed += cost
            plan.admitted.append(tile.tile_id)
            plan.logical_elapsed_ms = elapsed
            plan.peak_inflight_bytes = max(plan.peak_inflight_bytes,
                                           inflight)

        if not plan.admitted:
            # A valid configuration always admits at least one tile; guard
            # against pathological zero progress regardless.
            raise errors.fail(
                errors.E_BUDGET,
                "no tile could be admitted within the dual budget",
                round=round_index,
                blocked=blocked_on_memory.tile_id
                if blocked_on_memory else None)
        plans.append(plan)
        queue = [tile for tile in queue
                 if tile.tile_id in set(plan.deferred)]
        round_index += 1
    return plans


def execute_plans(
        task: TaskSpec,
        plans: List[RoundPlan],
        tile_by_id: Dict[str, TileSpec],
        worker: Callable[[TileSpec], None],
        stop_event: Optional[threading.Event] = None,
        ) -> ScheduleReport:
    """Run planned tiles with a bounded pool and memory semaphore.

    Within a round futures are created strictly in admission order; the
    semaphore enforces the same in-flight byte ceiling used by the planner.
    A graceful stop is honoured only on tile boundaries.
    """
    report = ScheduleReport(rounds=list(plans))
    memory = _MemoryLimiter(task.memory_budget_bytes)
    stop_event = stop_event or threading.Event()

    for plan in plans:
        with ThreadPoolExecutor(max_workers=task.max_concurrency) as pool:
            futures = []
            for tile_id in plan.admitted:
                if stop_event.is_set():
                    plan.deferred.extend(
                        tile_id for tile_id in plan.admitted
                        if tile_id not in {f.tile_id for f in futures})
                    break
                tile = tile_by_id[tile_id]
                buffer_bytes = sample_buffer_bytes(tile, task.samples)
                memory.acquire(buffer_bytes)

                def run_one(target: TileSpec = tile,
                            size: int = buffer_bytes) -> None:
                    try:
                        worker(target)
                    finally:
                        memory.release(size)

                futures.append(_FutureTile(tile_id, pool.submit(run_one)))

            for future in futures:
                tile = tile_by_id[future.tile_id]
                try:
                    future.future.result()
                except errors.EngineError as exc:
                    report.results.append(TileResult(
                        tile_id=tile.tile_id, ok=False,
                        round_index=plan.round_index,
                        logical_cost_ms=default_logical_cost_ms(
                            tile, task.samples),
                        buffer_bytes=sample_buffer_bytes(
                            tile, task.samples),
                        error_code=exc.code, error_message=exc.message))
                    continue
                report.results.append(TileResult(
                    tile_id=tile.tile_id, ok=True,
                    round_index=plan.round_index,
                    logical_cost_ms=default_logical_cost_ms(
                        tile, task.samples),
                    buffer_bytes=sample_buffer_bytes(tile, task.samples)))

        if stop_event.is_set():
            done_ids = {result.tile_id for result in report.results}
            for later_plan in plans[plan.round_index + 1:]:
                plan.deferred.extend(later_plan.admitted)
            plan.deferred.extend(
                tile_id for tile_id in plan.admitted
                if tile_id not in done_ids)
            plan.deferred = sorted(set(plan.deferred))
            break
    return report


class _FutureTile:
    def __init__(self, tile_id: str, future: object) -> None:
        self.tile_id = tile_id
        self.future = future

