"""Small ordered executor used by every concurrent phase."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Task:
    key: tuple[Any, ...]
    func: Callable[[], Any]
    label: str = ""


@dataclass(frozen=True)
class TaskResult:
    key: tuple[Any, ...]
    label: str
    value: Any = None
    exception: BaseException | None = None


def execute_ordered(tasks: list[Task], max_workers: int) -> tuple[list[TaskResult], list[tuple[Any, ...]]]:
    """Run tasks concurrently and return results sorted by stable task key.

    The second return value is the actual completion order, used only by tests and
    verification output to demonstrate that completion order is irrelevant.
    """

    if not tasks:
        return [], []
    ordered = sorted(tasks, key=lambda task: task.key)
    workers = max(1, min(int(max_workers), len(ordered)))
    by_future: dict[Any, Task] = {}
    results: list[TaskResult] = []
    completion_order: list[tuple[Any, ...]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="domain-editor") as pool:
        for task in ordered:
            by_future[pool.submit(task.func)] = task
        for future in as_completed(by_future):
            task = by_future[future]
            completion_order.append(task.key)
            try:
                value = future.result()
            except BaseException as exc:  # returned to caller for item-local handling
                results.append(TaskResult(task.key, task.label, exception=exc))
            else:
                results.append(TaskResult(task.key, task.label, value=value))
    results.sort(key=lambda result: result.key)
    return results, completion_order
