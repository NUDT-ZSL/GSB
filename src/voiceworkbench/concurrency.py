"""Deterministic thread-pool execution with per-item exception isolation.

Tasks carry stable sort keys.  Worker completion order is irrelevant: results
and failures are always reordered by key before returning, matching a
single-threaded run exactly.  A per-item exception never aborts the batch.

A process-wide shared pool is used so repeated batches (one per file, one per
pipeline stage) do not pay thread start/teardown costs.  The GIL means CPU
bound numpy work sees little speedup, but ordering and isolation semantics
remain identical to a serial run.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .errors import FailureRecord

_SHARED_POOLS: dict[int, ThreadPoolExecutor] = {}


def get_shared_pool(max_workers: int) -> ThreadPoolExecutor:
    pool = _SHARED_POOLS.get(max_workers)
    if pool is None:
        pool = ThreadPoolExecutor(max_workers=max_workers,
                                  thread_name_prefix=f"vb-pool-{max_workers}")
        _SHARED_POOLS[max_workers] = pool
    return pool


@dataclass
class Task:
    sort_key: list[Any]
    identifier: str
    stage: str
    payload: Any


@dataclass
class BatchOutcome:
    results: list[Any]
    failures: list[FailureRecord]


def run_tasks(
    tasks: Iterable[Task],
    worker: Callable[[Task], Any],
    max_workers: int,
) -> BatchOutcome:
    ordered = sorted(tasks, key=lambda item: item.sort_key)
    failures: list[FailureRecord] = []
    entries: list[tuple[list[Any], Any]] = []

    if max_workers <= 1 or len(ordered) <= 1:
        for task in ordered:
            result, failure = _run_one(task, worker)
            if failure is not None:
                failures.append(failure)
            else:
                entries.append((task.sort_key, result))
    else:
        pool = get_shared_pool(max_workers)
        future_to_task = {
            pool.submit(_run_one, task, worker): task for task in ordered
        }
        gathered: list[tuple[list[Any], Any, FailureRecord | None]] = []
        for future, task in future_to_task.items():
            result, failure = future.result()
            gathered.append((task.sort_key, result, failure))
        gathered.sort(key=lambda item: item[0])
        for key, result, failure in gathered:
            if failure is not None:
                failures.append(failure)
            else:
                entries.append((key, result))

    entries.sort(key=lambda item: item[0])
    failures.sort(key=lambda item: item.sort_key)
    return BatchOutcome(results=[result for _, result in entries], failures=failures)


def _run_one(task: Task, worker: Callable[[Task], Any]) -> tuple[Any, FailureRecord | None]:
    try:
        return worker(task), None
    except Exception as exc:  # intentional isolation boundary
        return None, FailureRecord(
            identifier=task.identifier,
            stage=task.stage,
            error_type=type(exc).__name__,
            code=getattr(exc, "code", "E_ITEM_FAILED"),
            message=str(exc),
            sort_key=list(task.sort_key),
        )
