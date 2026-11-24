from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .errors import E_MEMORY_BUDGET, WorkbenchError
from .geometry import Chunk


class LogicalClock:
    """Thread-safe injectable logical clock.

    The default clock only advances when :meth:`advance` is called, making
    scheduling simulations deterministic in tests.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def advance(self, milliseconds: float) -> float:
        with self._lock:
            self._now += float(milliseconds)
            return self._now

    def measure(
        self,
        work: Callable[[], Any],
        logical_ms: float,
        *,
        real_delay: float = 0.0,
    ) -> Any:
        result = work()
        if real_delay > 0:
            time.sleep(real_delay)
        self.advance(logical_ms)
        return result


@dataclass
class ScheduledRound:
    number: int
    chunks: List[str]
    memory_slots: int
    logical_ms: float
    time_budget_exceeded: bool = False
    stop_reason: str = "completed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round": self.number,
            "chunks": list(self.chunks),
            "admitted": len(self.chunks),
            "memory_slots": self.memory_slots,
            "logical_ms": self.logical_ms,
            "time_budget_exceeded": self.time_budget_exceeded,
            "stop_reason": self.stop_reason,
        }


@dataclass
class SchedulerPlan:
    rounds: List[ScheduledRound] = field(default_factory=list)

    @property
    def admission_sequence(self) -> List[str]:
        sequence: List[str] = []
        for round_info in self.rounds:
            sequence.extend(round_info.chunks)
        return sequence

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_count": len(self.rounds),
            "admission_sequence": self.admission_sequence,
            "rounds": [round_info.to_dict() for round_info in self.rounds],
        }


def plan_rounds(
    chunks: Sequence[Chunk],
    *,
    parallelism: int,
    memory_budget_slots: int,
    time_budget_ms: float,
    memory_cost: Callable[[Chunk], int],
    logical_cost: Callable[[Chunk], float],
) -> SchedulerPlan:
    """Create the reproducible admission plan.

    Chunks are considered in C-order (``z``, then ``y``, then ``x``). A round
    admits one batch that can run together in the bounded thread pool. The
    batch never exceeds parallelism or the summed pixel-buffer budget. The
    chunk that first makes accumulated logical time exceed the budget is
    admitted and then closes the round because chunk work is not preemptible.
    """
    pending = list(chunks)
    plan = SchedulerPlan()
    round_number = 0
    while pending:
        round_number += 1
        admitted: List[Chunk] = []
        used_memory = 0
        used_logical_ms = 0.0
        exceeded = False
        reason = "round_complete"

        while pending:
            candidate = pending[0]
            cost = int(memory_cost(candidate))
            if cost > memory_budget_slots:
                raise WorkbenchError(
                    E_MEMORY_BUDGET,
                    "pixel_memory_budget is smaller than one chunk buffer",
                    details={
                        "chunk": candidate.id,
                        "required_slots": cost,
                        "budget_slots": memory_budget_slots,
                    },
                )
            if len(admitted) >= int(parallelism):
                reason = "parallelism"
                break
            if used_memory + cost > memory_budget_slots:
                reason = "memory_budget"
                break

            pending.pop(0)
            admitted.append(candidate)
            used_memory += cost
            duration = float(logical_cost(candidate))
            used_logical_ms += duration
            if used_logical_ms > float(time_budget_ms):
                exceeded = True
                reason = "logic_time_budget"
                break

        if not admitted:  # defensive; cost > budget is caught above.
            raise WorkbenchError(
                E_MEMORY_BUDGET,
                "scheduler cannot admit the next chunk under memory budget",
                details={"budget_slots": memory_budget_slots},
            )

        if not exceeded and reason == "round_complete" and pending:
            # The inner loop stopped only because no candidates remain; if
            # pending exists, it must have hit a resource limit.
            reason = "resource_limit"
        plan.rounds.append(
            ScheduledRound(
                number=round_number,
                chunks=[chunk.id for chunk in admitted],
                memory_slots=used_memory,
                logical_ms=round(used_logical_ms, 6),
                time_budget_exceeded=exceeded,
                stop_reason=reason,
            )
        )
    return plan


@dataclass
class RunResult:
    completed: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    interrupted: bool = False
    plan: Optional[SchedulerPlan] = None


def run_plan(
    plan: SchedulerPlan,
    chunks_by_id: Dict[str, Chunk],
    *,
    parallelism: int,
    process_chunk: Callable[[Chunk], None],
    clock: Optional[LogicalClock] = None,
    stop_event: Optional[threading.Event] = None,
    chunk_real_delay: float = 0.0,
) -> RunResult:
    """Execute a planned round batch in a bounded thread pool."""
    clock = clock or LogicalClock()
    result = RunResult(plan=plan)

    for round_info in plan.rounds:
        if stop_event is not None and stop_event.is_set():
            result.interrupted = True
            return result

        chunk_objs = [chunks_by_id[chunk_id] for chunk_id in round_info.chunks]
        max_workers = max(1, min(int(parallelism), len(chunk_objs)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {}
            for chunk in chunk_objs:
                duration = round_info.logical_ms / max(1, len(chunk_objs))

                def job(c: Chunk = chunk, d: float = duration) -> None:
                    if chunk_real_delay > 0:
                        time.sleep(chunk_real_delay)
                    clock.measure(lambda: process_chunk(c), d)

                future_map[executor.submit(job)] = chunk.id

            for future in list(future_map):
                chunk_id = future_map[future]
                try:
                    future.result()
                    result.completed.append(chunk_id)
                except Exception as exc:  # surfaced in report/CLI
                    result.failed[chunk_id] = f"{type(exc).__name__}: {exc}"

        result.completed.sort()
        if result.failed:
            return result
        if stop_event is not None and stop_event.is_set():
            result.interrupted = True
            return result

    return result
