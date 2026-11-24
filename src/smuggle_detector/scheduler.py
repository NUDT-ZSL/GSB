"""Bounded, deterministic batch admission with real wall-clock deadlines."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable

from .constants import E_BUDGET_STOPPED, E_QUEUE_FULL, E_TIMEOUT
from .detector import failed_connection_result, internal_failure_result
from .errors import DeadlineExceeded


@dataclass(frozen=True)
class SchedulerConfig:
    concurrency: int
    connection_timeout_ms: int
    batch_budget_ms: int
    queue_limit: int


Worker = Callable[[str, int], dict]


def _queue_result(connection_id: str, queue_limit: int) -> dict:
    result = failed_connection_result(
        connection_id,
        code=E_QUEUE_FULL,
        detail=f"batch admission queue limit exceeded (queue_limit={queue_limit})",
    )
    result["status"] = "queue_full"
    return result


def _budget_result(connection_id: str, budget_ms: int) -> dict:
    result = failed_connection_result(
        connection_id,
        code=E_BUDGET_STOPPED,
        detail=f"batch budget exhausted before admission (budget_ms={budget_ms})",
    )
    result["status"] = "budget_stopped"
    return result


def _timeout_result(
    connection_id: str,
    *,
    elapsed_ms: float,
    timeout_ms: int,
) -> dict:
    return failed_connection_result(
        connection_id,
        code=E_TIMEOUT,
        detail="connection exceeded real wall-clock processing timeout",
        elapsed_ms=elapsed_ms,
        timeout_ms=timeout_ms,
    )


def _execute_worker(
    worker: Worker,
    connection_id: str,
    timeout_ms: int,
) -> dict:
    started = time.perf_counter()
    try:
        result = worker(connection_id, timeout_ms)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms > timeout_ms:
            return _timeout_result(
                connection_id,
                elapsed_ms=elapsed_ms,
                timeout_ms=timeout_ms,
            )
        result["scheduler_elapsed_ms"] = round(elapsed_ms, 3)
        return result
    except DeadlineExceeded:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return _timeout_result(
            connection_id,
            elapsed_ms=elapsed_ms,
            timeout_ms=timeout_ms,
        )
    except Exception as exc:  # one connection must not stop the batch
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return internal_failure_result(
            connection_id,
            detail=f"{type(exc).__name__}: {exc}",
            elapsed_ms=elapsed_ms,
        )


def summarize_results(results: Iterable[dict]) -> dict:
    result_list = list(results)
    return {
        "task_count": len(result_list),
        "success_count": sum(1 for item in result_list if item["status"] == "ok"),
        "timeout_count": sum(
            1 for item in result_list if item["status"] == "timeout"
        ),
        "queue_rejected_count": sum(
            1 for item in result_list if item["status"] == "queue_full"
        ),
        "budget_stopped_count": sum(
            1 for item in result_list if item["status"] == "budget_stopped"
        ),
        "failure_count": sum(
            1 for item in result_list if item["status"] == "failed"
        ),
        "hit_count": sum(len(item.get("hits", [])) for item in result_list),
        "error_count": sum(len(item.get("errors", [])) for item in result_list),
        "duplicate_delivery_count": sum(
            item.get("duplicate_count", 0) for item in result_list
        ),
    }


def run_batch(
    connection_ids: Iterable[str],
    *,
    existing_results: dict[str, dict] | None,
    config: SchedulerConfig,
    worker: Worker,
    on_result: Callable[[dict], None] | None = None,
) -> tuple[list[dict], dict, dict]:
    """Run a stable batch admission window.

    Admission capacity is ``concurrency + queue_limit``.  Because the input is
    a fixed batch, admission is decided up front in stable connection-id order:
    this makes queue rejection and report ordering independent of task
    completion order.  Work already admitted is never cancelled by budget
    exhaustion.
    """

    ordered = sorted(set(connection_ids))
    existing = dict(existing_results or {})
    total = len(ordered)
    admission_capacity = min(total, config.concurrency + config.queue_limit)
    accepted = ordered[:admission_capacity]
    overflow = ordered[admission_capacity:]

    results: dict[str, dict] = {
        connection_id: existing[connection_id]
        for connection_id in ordered
        if connection_id in existing
    }
    budget_stopped = False
    started = time.perf_counter()

    def publish(result: dict) -> None:
        results[result["connection_id"]] = result
        if on_result is not None:
            on_result(result)

    for connection_id in overflow:
        publish(_queue_result(connection_id, config.queue_limit))

    submitted: list[tuple[str, object]] = []
    submitted_ids: set[str] = set()
    executor = ThreadPoolExecutor(
        max_workers=config.concurrency,
        thread_name_prefix="smuggle-detect",
    )
    try:
        for connection_id in accepted:
            if connection_id in results:
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if elapsed_ms >= config.batch_budget_ms:
                budget_stopped = True
                break
            future = executor.submit(
                _execute_worker,
                worker,
                connection_id,
                config.connection_timeout_ms,
            )
            submitted.append((connection_id, future))
            submitted_ids.add(connection_id)

        if budget_stopped:
            pending = [
                connection_id
                for connection_id in accepted
                if connection_id not in results and connection_id not in submitted_ids
            ]
            for connection_id in pending:
                publish(_budget_result(connection_id, config.batch_budget_ms))

        future_map = {
            future: connection_id for connection_id, future in submitted
        }
        for future in as_completed(future_map):
            connection_id = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                result = internal_failure_result(
                    connection_id,
                    detail=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=0.0,
                )
            publish(result)
    finally:
        executor.shutdown(wait=True, cancel_futures=False)

    ordered_results = [results[connection_id] for connection_id in ordered]
    summary = summarize_results(ordered_results)
    scheduling = {
        "concurrency": config.concurrency,
        "connection_timeout_ms": config.connection_timeout_ms,
        "batch_budget_ms": config.batch_budget_ms,
        "queue_limit": config.queue_limit,
        "admission_capacity": admission_capacity,
        "budget_stopped": budget_stopped,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "resumed_results": len(existing),
    }
    return ordered_results, summary, scheduling

