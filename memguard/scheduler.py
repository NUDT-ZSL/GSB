"""Arena-level bounded parallel scheduling with real resource measurements."""

from __future__ import annotations

import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Mapping

from .codes import E_ARENA_TASK, E_BUDGET, E_QUEUE_FULL, E_TIMEOUT
from .evaluator import evaluate_access
from .models import ArenaRun, Region, Rejection, TraceRef
from .parser import load_trace_refs


@dataclass(frozen=True)
class ScheduleConfig:
    max_workers: int = 4
    max_inflight_bytes: int = 32 * 1024 * 1024
    arena_timeout_ms: float = 5000.0
    max_pending: int = 64


@dataclass
class _ArenaGroup:
    arena_id: str
    refs: list[TraceRef]
    reserved_bytes: int
    degradation: set[str] = field(default_factory=set)


@dataclass
class _TaskResult:
    arena_id: str
    records: tuple
    verdicts: tuple
    loaded_bytes: int
    elapsed_ms: float
    rejections: tuple[Rejection, ...]


def _execute_arena(
    group: _ArenaGroup, regions: Mapping[str, Region]
) -> _TaskResult:
    started = time.perf_counter()
    records, rejections, loaded_bytes = load_trace_refs(group.refs, regions)
    verdicts = tuple(evaluate_access(record, regions) for record in records)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return _TaskResult(
        arena_id=group.arena_id,
        records=tuple(records),
        verdicts=verdicts,
        loaded_bytes=loaded_bytes,
        elapsed_ms=elapsed_ms,
        rejections=tuple(rejections),
    )


def schedule_arenas(
    refs_by_arena: Mapping[str, list[TraceRef]],
    regions: Mapping[str, Region],
    config: ScheduleConfig,
) -> tuple[list[ArenaRun], list[Rejection]]:
    if config.max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    if config.max_inflight_bytes < 0:
        raise ValueError("max_inflight_bytes must be non-negative")
    if config.arena_timeout_ms <= 0:
        raise ValueError("arena_timeout_ms must be positive")
    if config.max_pending < 1:
        raise ValueError("max_pending must be at least 1")

    groups: list[_ArenaGroup] = []
    for arena_id in sorted(refs_by_arena):
        refs = refs_by_arena[arena_id]
        reserved = sum(ref.byte_length for ref in refs)
        group = _ArenaGroup(arena_id=arena_id, refs=refs, reserved_bytes=reserved)
        if reserved > config.max_inflight_bytes:
            group.degradation.add(E_BUDGET)
        groups.append(group)

    pending = deque(groups)
    running: dict[Future[_TaskResult], tuple[_ArenaGroup, float]] = {}
    results: list[_TaskResult] = []
    scheduler_rejections: list[Rejection] = []
    in_flight_bytes = 0
    completion_order: list[str] = []
    queue_high_water = 0

    with ThreadPoolExecutor(max_workers=config.max_workers, thread_name_prefix="memguard") as pool:
        while pending or running:
            queue_high_water = max(queue_high_water, len(pending))
            if len(pending) > config.max_pending:
                for waiting in pending:
                    waiting.degradation.add(E_QUEUE_FULL)

            started_any = False
            while pending and len(running) < config.max_workers:
                candidate = pending[0]
                projected = in_flight_bytes + candidate.reserved_bytes
                if running and (
                    projected > config.max_inflight_bytes
                    or candidate.reserved_bytes > config.max_inflight_bytes
                ):
                    break
                pending.popleft()
                future = pool.submit(_execute_arena, candidate, regions)
                running[future] = (candidate, time.perf_counter())
                in_flight_bytes += candidate.reserved_bytes
                started_any = True

            if not running and pending:
                candidate = pending.popleft()
                candidate.degradation.add(E_BUDGET)
                future = pool.submit(_execute_arena, candidate, regions)
                running[future] = (candidate, time.perf_counter())
                in_flight_bytes += candidate.reserved_bytes
                started_any = True

            if not running:
                if started_any:
                    continue
                break

            completed, _ = wait(
                tuple(running),
                timeout=0.05,
                return_when=FIRST_COMPLETED,
            )
            if not completed:
                continue
            for future in completed:
                group, _started_at = running.pop(future)
                in_flight_bytes -= group.reserved_bytes
                if in_flight_bytes < 0:
                    in_flight_bytes = 0
                try:
                    result = future.result()
                except Exception as exc:  # keep other arenas running
                    scheduler_rejections.append(
                        Rejection(
                            source_file="<arena:%s>" % group.arena_id,
                            line_number=None,
                            business_code=E_ARENA_TASK,
                            message="arena task failed",
                            detail={"arena_id": group.arena_id, "error": repr(exc)},
                        )
                    )
                    group.degradation.add(E_ARENA_TASK)
                    result = _TaskResult(
                        arena_id=group.arena_id,
                        records=(),
                        verdicts=(),
                        loaded_bytes=0,
                        elapsed_ms=0.0,
                        rejections=(),
                    )
                if result.elapsed_ms > config.arena_timeout_ms:
                    group.degradation.add(E_TIMEOUT)
                if result.loaded_bytes != group.reserved_bytes:
                    group.degradation.add(E_BUDGET)
                    scheduler_rejections.append(
                        Rejection(
                            source_file="<arena:%s>" % group.arena_id,
                            line_number=None,
                            business_code=E_BUDGET,
                            message="actual arena-loaded bytes differ from reserved bytes",
                            detail={
                                "arena_id": group.arena_id,
                                "reserved_bytes": group.reserved_bytes,
                                "loaded_bytes": result.loaded_bytes,
                            },
                        )
                    )
                results.append(result)
                completion_order.append(group.arena_id)
                for rejection in result.rejections:
                    scheduler_rejections.append(rejection)

    arena_runs: list[ArenaRun] = []
    result_by_arena = {result.arena_id: result for result in results}
    for group in groups:
        result = result_by_arena.get(group.arena_id)
        if result is None:
            group.degradation.add(E_ARENA_TASK)
            result = _TaskResult(
                arena_id=group.arena_id,
                records=(),
                verdicts=(),
                loaded_bytes=0,
                elapsed_ms=0.0,
                rejections=(),
            )
        arena_runs.append(
            ArenaRun(
                arena_id=group.arena_id,
                records=result.records,
                verdicts=result.verdicts,
                loaded_bytes=result.loaded_bytes,
                elapsed_ms=result.elapsed_ms,
                degradation_codes=tuple(sorted(group.degradation)),
                rejections=result.rejections,
            )
        )

    # Completion order is deliberately not used to order the final results.
    _ = completion_order
    _ = queue_high_water
    scheduler_rejections.sort(key=lambda item: item.sort_key)
    arena_runs.sort(key=lambda item: item.arena_id)
    return arena_runs, scheduler_rejections
