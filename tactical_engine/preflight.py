from __future__ import annotations

import threading
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import EngineError
from .model import Candidate, Point, Scenario, UnitSpec
from .planner import PlanResult, Planner, SearchCancelled


def estimate_workspace_bytes(cell_count: int) -> int:
    return cell_count * (8 + 8 + 8 + 4 + 1)


class MemoryLimiter:
    def __init__(self, limit_bytes: int):
        self.limit_bytes = limit_bytes
        self.used_bytes = 0
        self.peak_bytes = 0
        self._condition = threading.Condition()

    def reserve(self, amount: int) -> None:
        if amount <= 0:
            return
        if amount > self.limit_bytes:
            raise EngineError(
                "E_MEMORY_LIMIT",
                "single preflight workspace exceeds the memory budget",
                {"required_bytes": amount, "limit_bytes": self.limit_bytes},
            )
        with self._condition:
            while self.used_bytes + amount > self.limit_bytes:
                self._condition.wait()
            self.used_bytes += amount
            self.peak_bytes = max(self.peak_bytes, self.used_bytes)

    def release(self, amount: int) -> None:
        if amount <= 0:
            return
        with self._condition:
            self.used_bytes -= amount
            self._condition.notify_all()


class PreflightCancellation:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.cancel_after: int | None = None

    def cancel_from(self, index: int) -> None:
        with self._lock:
            if self.cancel_after is None or index < self.cancel_after:
                self.cancel_after = index

    def is_cancelled(self, index: int) -> bool:
        with self._lock:
            return self.cancel_after is not None and index > self.cancel_after


@dataclass
class Workspace:
    cost_buffer: np.ndarray
    g_buffer: np.ndarray
    reference_buffer: np.ndarray
    parent_buffer: np.ndarray
    closed_buffer: np.ndarray

    @property
    def nbytes(self) -> int:
        return (
            self.cost_buffer.nbytes
            + self.g_buffer.nbytes
            + self.reference_buffer.nbytes
            + self.parent_buffer.nbytes
            + self.closed_buffer.nbytes
        )


def _allocate_workspace(cell_count: int) -> Workspace:
    return Workspace(
        cost_buffer=np.zeros(cell_count, dtype=np.float64),
        g_buffer=np.full(cell_count, np.inf, dtype=np.float64),
        reference_buffer=np.zeros(cell_count, dtype=np.float64),
        parent_buffer=np.full(cell_count, -1, dtype=np.int32),
        closed_buffer=np.zeros(cell_count, dtype=np.bool_),
    )


@dataclass(frozen=True)
class CandidatePreflightResult:
    candidate_id: str
    cell: Point
    reachable: bool
    cost_fixed: int
    expanded_count: int
    generated_count: int
    path: tuple[Point, ...]
    expanded_path: tuple[Point, ...]
    error_bound: float
    workspace_bytes: int
    plan: PlanResult

    def core_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "cell": list(self.cell),
            "reachable": self.reachable,
            "cost_fixed": self.cost_fixed,
            "expanded_count": self.expanded_count,
            "generated_count": self.generated_count,
            "path": [list(point) for point in self.path],
            "expanded_path": [list(point) for point in self.expanded_path],
            "error_bound": self.error_bound,
            "workspace_bytes": self.workspace_bytes,
        }


@dataclass(frozen=True)
class PreflightReport:
    unit_id: str
    concurrency: int
    memory_budget_bytes: int
    peak_buffer_bytes: int
    merge_order: tuple[str, ...]
    completion_order: tuple[str, ...]
    results: tuple[CandidatePreflightResult, ...]
    selected_candidate_id: str | None
    selected_path: tuple[Point, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "concurrency": self.concurrency,
            "memory_budget_bytes": self.memory_budget_bytes,
            "peak_buffer_bytes": self.peak_buffer_bytes,
            "merge_order": list(self.merge_order),
            "completion_order_diagnostic_only": list(self.completion_order),
            "results": [result.core_dict() for result in self.results],
            "selected_candidate_id": self.selected_candidate_id,
            "selected_path": [list(point) for point in self.selected_path],
        }


def _run_candidate(
    index: int,
    scenario: Scenario,
    unit: UnitSpec,
    candidate: Candidate,
    limiter: MemoryLimiter,
    cancellation: PreflightCancellation,
) -> CandidatePreflightResult:
    cell_count = scenario.terrain.width * scenario.terrain.height
    reserved_bytes = estimate_workspace_bytes(cell_count)
    limiter.reserve(reserved_bytes)
    workspace: Workspace | None = None
    try:
        workspace = _allocate_workspace(cell_count)
        if cancellation.is_cancelled(index):
            raise SearchCancelled("preflight cancelled")
        planner = Planner(scenario)
        plan = planner.plan(
            unit.start,
            candidate.cell,
            unit.budget_fixed,
            unit_id=unit.id,
            cancelled=lambda: cancellation.is_cancelled(index),
        )
        return CandidatePreflightResult(
            candidate_id=candidate.id,
            cell=candidate.cell,
            reachable=bool(plan.cost.steps),
            cost_fixed=plan.cost.fixed_total,
            expanded_count=plan.optimal_search.expanded_count,
            generated_count=plan.optimal_search.generated_count,
            path=plan.path,
            expanded_path=tuple(step.cell for step in plan.cost.steps),
            error_bound=plan.cost.error_bound,
            workspace_bytes=workspace.nbytes,
            plan=plan,
        )
    finally:
        limiter.release(reserved_bytes)


def _as_engine_error(candidate: Candidate, exc: BaseException) -> EngineError:
    if isinstance(exc, EngineError):
        return exc
    return EngineError(
        "E_PREFLIGHT_FAILED",
        "candidate reachability preflight failed",
        {
            "candidate_id": candidate.id,
            "exception": type(exc).__name__,
            "reason": str(exc),
        },
    )


def preflight_unit(
    scenario: Scenario,
    unit: UnitSpec,
    *,
    concurrency: int | None = None,
    memory_budget_bytes: int | None = None,
) -> PreflightReport:
    worker_count = min(
        concurrency if concurrency is not None else scenario.concurrency,
        len(unit.candidates),
    )
    worker_count = max(1, worker_count)
    memory_budget = (
        memory_budget_bytes
        if memory_budget_bytes is not None
        else scenario.memory_budget_bytes
    )
    limiter = MemoryLimiter(memory_budget)
    cancellation = PreflightCancellation()
    results_by_index: dict[int, CandidatePreflightResult] = {}
    errors_by_index: dict[int, EngineError] = {}
    completion_order: list[str] = []
    candidates = unit.candidates

    executor = ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix=f"preflight-{unit.id}",
    )
    futures: dict[Future[CandidatePreflightResult], int] = {}
    try:
        for index, candidate in enumerate(candidates):
            futures[
                executor.submit(
                    _run_candidate,
                    index,
                    scenario,
                    unit,
                    candidate,
                    limiter,
                    cancellation,
                )
            ] = index
        for future in as_completed(futures):
            index = futures[future]
            candidate = candidates[index]
            try:
                result = future.result()
                completion_order.append(candidate.id)
                results_by_index[index] = result
            except CancelledError:
                continue
            except SearchCancelled:
                continue
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                errors_by_index[index] = _as_engine_error(candidate, exc)
                cancellation.cancel_from(index)
                for pending, pending_index in futures.items():
                    if pending_index > (cancellation.cancel_after or 0):
                        pending.cancel()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    if errors_by_index:
        first_index = min(errors_by_index)
        raise errors_by_index[first_index]

    ordered_results = tuple(results_by_index[index] for index in range(len(candidates)))
    reachable = [result for result in ordered_results if result.reachable]
    selected: CandidatePreflightResult | None = None
    if reachable:
        selected = min(
            reachable,
            key=lambda result: (
                result.cost_fixed,
                result.candidate_id,
                result.cell,
                tuple(result.expanded_path),
            ),
        )
    return PreflightReport(
        unit_id=unit.id,
        concurrency=worker_count,
        memory_budget_bytes=memory_budget,
        peak_buffer_bytes=limiter.peak_bytes,
        merge_order=tuple(candidate.id for candidate in candidates),
        completion_order=tuple(completion_order),
        results=ordered_results,
        selected_candidate_id=selected.candidate_id if selected else None,
        selected_path=selected.path if selected else (),
    )
