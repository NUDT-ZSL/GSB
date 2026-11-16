"""Thread-pool scheduling with deterministic per-instance ordering."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from .errors import E_INSTANCE_TASK_FAILED, PipelineError
from .models import FailureRecord

T = TypeVar("T")


def stable_sort_key(instance_id: str, stage: str) -> str:
    """Deterministic ordering key derived from instance id and phase name."""

    return f"{instance_id}::{stage}"


@dataclass
class Task(Generic[T]):
    instance_id: str
    stage: str
    func: Callable[[], T]

    @property
    def sort_key(self) -> str:
        return stable_sort_key(self.instance_id, self.stage)


@dataclass
class TaskBatchResult(Generic[T]):
    successes: list[tuple[str, str, T]]
    failures: list[FailureRecord]


def run_tasks(tasks: list[Task[Any]], workers: int) -> TaskBatchResult[Any]:
    """Run tasks with a bounded thread pool while keeping output deterministic.

    The pool may reorder execution, but every result is re-sorted by the stable
    ``instance_id::stage`` key so single-threaded and multi-threaded runs are
    byte-identical.  Exceptions never escape a task; they become failure
    records and processing continues for every remaining instance.
    """

    ordered = sorted(tasks, key=lambda task: task.sort_key)
    successes: list[tuple[str, str, Any]] = []
    failures: list[FailureRecord] = []
    if workers <= 1:
        for task in ordered:
            _execute(task, successes, failures)
        return TaskBatchResult(successes=successes, failures=failures)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(_safe_run, task): task for task in ordered}
        for future in as_completed(future_map):
            task = future_map[future]
            outcome = future.result()
            if isinstance(outcome, BaseException):
                failures.append(_failure_record(task, outcome))
            else:
                successes.append((task.instance_id, task.stage, outcome))
    successes.sort(key=lambda item: stable_sort_key(item[0], item[1]))
    failures.sort(key=lambda item: item.sort_key)
    return TaskBatchResult(successes=successes, failures=failures)


def _safe_run(task: Task[Any]) -> Any:
    try:
        return task.func()
    except BaseException as exc:  # isolation boundary: never kill the pool
        return exc


def _execute(task: Task[Any], successes: list[tuple[str, str, Any]], failures: list[FailureRecord]) -> None:
    try:
        successes.append((task.instance_id, task.stage, task.func()))
    except BaseException as exc:
        failures.append(_failure_record(task, exc))


def _failure_record(task: Task[Any], exc: BaseException) -> FailureRecord:
    if isinstance(exc, PipelineError):
        code = exc.code
        message = exc.message
        details = exc.details
    else:
        code = E_INSTANCE_TASK_FAILED
        message = str(exc) or repr(exc)
        details = {}
    return FailureRecord(
        instance_id=task.instance_id,
        stage=task.stage,
        code=code,
        exception_type=type(exc).__name__,
        message=message,
        sort_key=task.sort_key,
        details=details,
    )
