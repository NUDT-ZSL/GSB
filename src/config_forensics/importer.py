"""Bounded concurrent file import with measured per-file time budgets."""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .codes import Code
from .issues import Issue
from .models import FileResult


@dataclass(frozen=True)
class FileTask:
    source_id: str
    source_name: str
    path: Path
    file_index: int
    sha256: str


@dataclass
class ImportOutcome:
    results: dict[str, FileResult] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    admitted_count: int = 0
    queue_rejected_count: int = 0
    timed_out_count: int = 0


def _generic_failure(task: FileTask, exc: BaseException) -> FileResult:
    return FileResult(
        source_id=task.source_id,
        source_name=task.source_name,
        file_index=task.file_index,
        file_id=task.source_name,
        sha256=task.sha256,
        status="rejected",
        issues=[
            Issue(
                Code.FILE_READ,
                file_id=task.source_name,
                file_index=task.file_index,
                details={"reason": f"unexpected processor failure: {exc}"},
            )
        ],
    )


def _queue_rejected(task: FileTask) -> FileResult:
    return FileResult(
        source_id=task.source_id,
        source_name=task.source_name,
        file_index=task.file_index,
        file_id=task.source_name,
        sha256=task.sha256,
        status="queue_rejected",
        issues=[
            Issue(
                Code.QUEUE_FULL,
                file_id=task.source_name,
                file_index=task.file_index,
                details={"admission_rejected": True},
            )
        ],
    )


def _timed_out(task: FileTask, maximum_seconds: float, measured: float) -> FileResult:
    return FileResult(
        source_id=task.source_id,
        source_name=task.source_name,
        file_index=task.file_index,
        file_id=task.source_name,
        sha256=task.sha256,
        status="timed_out",
        issues=[
            Issue(
                Code.FILE_TIMEOUT,
                file_id=task.source_name,
                file_index=task.file_index,
                details={
                    "max_seconds": maximum_seconds,
                    "thread_termination_claimed": False,
                    "chain_records_discarded": True,
                },
            )
        ],
    )


def import_files(
    tasks: list[FileTask],
    *,
    workers: int,
    queue_limit: int,
    max_file_seconds: float,
    processor: Callable[[FileTask], FileResult],
    on_result: Callable[[FileResult], None] | None = None,
) -> ImportOutcome:
    """Import files concurrently while keeping the pending window bounded.

    The semaphore covers active workers plus the configured waiting queue. If no
    slot is immediately available, that file is deterministically rejected with
    E_QUEUE_FULL rather than blocking or growing an unbounded executor queue.
    """
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if queue_limit < 0:
        raise ValueError("queue_limit must be non-negative")
    if max_file_seconds <= 0:
        raise ValueError("max_file_seconds must be positive")

    outcome = ImportOutcome()
    capacity = workers + queue_limit
    admission = threading.BoundedSemaphore(capacity)
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="evidence-import")
    future_meta: dict[Future[tuple[FileResult, float]], tuple[FileTask, float]] = {}
    pending: set[Future[tuple[FileResult, float]]] = set()

    def release_slot(_future: Future[tuple[FileResult, float]]) -> None:
        try:
            admission.release()
        except ValueError:
            pass

    def invoke(task: FileTask) -> tuple[FileResult, float]:
        started = time.monotonic()
        try:
            result = processor(task)
        except BaseException as exc:
            result = _generic_failure(task, exc)
        return result, time.monotonic() - started

    try:
        for task in tasks:
            if not admission.acquire(blocking=False):
                result = _queue_rejected(task)
                outcome.results[task.source_id] = result
                outcome.queue_rejected_count += 1
                if on_result is not None:
                    on_result(result)
                continue
            try:
                future = executor.submit(invoke, task)
            except BaseException:
                admission.release()
                raise
            started = time.monotonic()
            future.add_done_callback(release_slot)
            future_meta[future] = (task, started)
            pending.add(future)
            outcome.admitted_count += 1

        while pending:
            now = time.monotonic()
            completed_now = [future for future in pending if future.done()]
            if completed_now:
                for future in sorted(
                    completed_now,
                    key=lambda item: future_meta[item][0].source_id,
                ):
                    task, _submitted = future_meta.pop(future)
                    pending.remove(future)
                    try:
                        result, measured = future.result()
                    except BaseException as exc:
                        result = _generic_failure(task, exc)
                        measured = time.monotonic() - _submitted
                    if measured > max_file_seconds:
                        result = _timed_out(task, max_file_seconds, measured)
                        outcome.timed_out_count += 1
                    outcome.results[task.source_id] = result
                    outcome.timings[task.source_id] = measured
                    if on_result is not None:
                        on_result(result)
                continue

            timed_out_futures = [
                future
                for future in pending
                if now - future_meta[future][1] >= max_file_seconds
            ]
            if timed_out_futures:
                for future in sorted(
                    timed_out_futures,
                    key=lambda item: future_meta[item][0].source_id,
                ):
                    task, started = future_meta.pop(future)
                    pending.remove(future)
                    measured = time.monotonic() - started
                    future.cancel()
                    result = _timed_out(task, max_file_seconds, measured)
                    outcome.results[task.source_id] = result
                    outcome.timings[task.source_id] = measured
                    outcome.timed_out_count += 1
                    if on_result is not None:
                        on_result(result)
                continue

            next_deadline = min(
                future_meta[future][1] + max_file_seconds for future in pending
            )
            wait_timeout = max(0.0, next_deadline - time.monotonic())
            done, _ = wait(
                pending,
                timeout=wait_timeout,
                return_when=FIRST_COMPLETED,
            )
            for future in sorted(
                done,
                key=lambda item: future_meta[item][0].source_id,
            ):
                task, started = future_meta.pop(future)
                pending.remove(future)
                try:
                    result, measured = future.result()
                except BaseException as exc:
                    result = _generic_failure(task, exc)
                    measured = time.monotonic() - started
                if measured > max_file_seconds:
                    result = _timed_out(task, max_file_seconds, measured)
                    outcome.timed_out_count += 1
                outcome.results[task.source_id] = result
                outcome.timings[task.source_id] = measured
                if on_result is not None:
                    on_result(result)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return outcome
