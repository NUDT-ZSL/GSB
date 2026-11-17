"""Bounded thread-pool execution with stable result ordering and graceful stop."""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import BusinessRuleError, E_STAGE_EXCEPTION, Failure


@dataclass
class Task:
    key: str
    sort_key: tuple[Any, ...]
    stage: str
    label: str
    loader: Callable[[], Any | None]
    calculator: Callable[[], Any]
    view_index: int | None = None
    pair: tuple[int, int] | None = None
    injection_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskBatchResult:
    payloads: list[Any]
    failures: list[Failure]
    completed_keys: list[str]
    completed_views: list[int]
    interrupted: bool


def _failure_for_exception(task: Task, error: BaseException) -> Failure:
    if isinstance(error, BusinessRuleError):
        return error.failure
    return Failure(
        code=E_STAGE_EXCEPTION,
        stage=task.stage,
        message=f"task failed: {type(error).__name__}: {error}",
        view_index=task.view_index,
        pair=task.pair,
        injection_ids=task.injection_ids,
        detail={"task_key": task.key, "task_label": task.label},
    )


def execute_tasks(
    tasks: list[Task],
    *,
    max_workers: int,
    stop_event: Any,
    on_complete: Callable[[Task, Any, int, int], None],
    test_delay_ms: int = 0,
) -> TaskBatchResult:
    ordered = sorted(tasks, key=lambda task: (task.sort_key, task.key))
    results: list[tuple[tuple[Any, ...], str, Any]] = []
    failures: list[Failure] = []
    completed_keys: list[str] = []
    completed_views: set[int] = set()
    pending = list(ordered)
    total = len(pending)
    completion_count = 0

    def accept(task: Task, payload: Any) -> None:
        nonlocal completion_count
        results.append((task.sort_key, task.key, payload))
        completed_keys.append(task.key)
        if task.view_index is not None:
            completed_views.add(int(task.view_index))
        for value in task.metadata.get("completed_views", []):
            completed_views.add(int(value))
        completion_count += 1
        on_complete(task, payload, completion_count, total)

    # Cache hits are loaded on the coordinator thread before any worker starts.
    work_queue: list[Task] = []
    for task in pending:
        if bool(stop_event.is_set()):
            work_queue = []
            break
        try:
            cached = task.loader()
        except Exception:
            cached = None
        if cached is None:
            work_queue.append(task)
        else:
            accept(task, cached)

    interrupted = bool(stop_event.is_set())
    if not work_queue:
        return TaskBatchResult(
            payloads=[payload for _, _, payload in sorted(results, key=lambda item: (item[0], item[1]))],
            failures=failures,
            completed_keys=sorted(set(completed_keys)),
            completed_views=sorted(completed_views),
            interrupted=interrupted,
        )

    def calculate(task: Task) -> Any:
        if test_delay_ms > 0:
            time.sleep(test_delay_ms / 1000.0)
        return task.calculator()

    executor = ThreadPoolExecutor(
        max_workers=max(1, int(max_workers)),
        thread_name_prefix="recon",
    )
    futures: dict[Future[Any], Task] = {}
    next_index = 0
    try:
        while next_index < len(work_queue) and len(futures) < max(1, int(max_workers)):
            task = work_queue[next_index]
            next_index += 1
            futures[executor.submit(calculate, task)] = task
        while futures:
            if bool(stop_event.is_set()):
                interrupted = True
            try:
                done_futures, _ = wait(futures.keys(), return_when=FIRST_COMPLETED)
            except KeyboardInterrupt:
                stop_event.set()
                interrupted = True
                continue
            for future in done_futures:
                task = futures.pop(future)
                try:
                    payload = future.result()
                except Exception as error:
                    failures.append(_failure_for_exception(task, error))
                    continue
                accept(task, payload)
            if not bool(stop_event.is_set()):
                while next_index < len(work_queue) and len(futures) < max(1, int(max_workers)):
                    task = work_queue[next_index]
                    next_index += 1
                    futures[executor.submit(calculate, task)] = task
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    payloads = [
        payload for _, _, payload in sorted(results, key=lambda item: (item[0], item[1]))
    ]
    return TaskBatchResult(
        payloads=payloads,
        failures=failures,
        completed_keys=sorted(set(completed_keys)),
        completed_views=sorted(completed_views),
        interrupted=interrupted or bool(stop_event.is_set()),
    )
