"""Deterministic admission and real wall-clock bounded task scheduling."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .errors import (
    E_BUDGET,
    E_INTERNAL,
    E_QUEUE_FULL,
    E_TIMEOUT,
    TaskTimeout,
)
from .models import ChainDeclaration


@dataclass(frozen=True)
class SchedulerConfig:
    workers: int = 4
    max_tasks: int = 1_000_000
    queue_limit: int = 1_000_000
    task_timeout_ms: int | None = None

    def __post_init__(self) -> None:
        if self.workers < 1:
            raise ValueError("workers must be at least 1")
        if self.max_tasks < 0:
            raise ValueError("max_tasks must be non-negative")
        if self.queue_limit < 1:
            raise ValueError("queue_limit must be at least 1")
        if self.task_timeout_ms is not None and self.task_timeout_ms <= 0:
            raise ValueError("task_timeout_ms must be positive")

    def fingerprint(self) -> dict:
        return {
            "max_tasks": self.max_tasks,
            "queue_limit": self.queue_limit,
            "task_timeout_ms": self.task_timeout_ms,
        }


@dataclass
class SchedulerOutcome:
    results: dict[str, dict]
    executed_chain_ids: list[str] = field(default_factory=list)
    reused_chain_ids: list[str] = field(default_factory=list)


TaskFunction = Callable[[ChainDeclaration, float], dict]
ProgressCallback = Callable[[dict[str, dict]], None]
TaskObserver = Callable[[dict], None]


def _base_result(chain: ChainDeclaration) -> dict:
    return {
        "chain_id": chain.chain_id,
        "certificate_ids": list(chain.certificate_ids),
        "deadline_ms": chain.deadline_ms,
        "expected_summary": chain.expected_summary,
        "source_file": chain.source_file,
        "entry_index": chain.entry_index,
    }


def _task_error(code: str, reason: str, message: str) -> list[dict]:
    return [
        {
            "code": code,
            "position": None,
            "cert_id": None,
            "reason": reason,
            "message": message,
        }
    ]


def _run_task(
    chain: ChainDeclaration,
    task_function: TaskFunction,
    config: SchedulerConfig,
) -> dict:
    result = _base_result(chain)
    effective_timeout_ms = chain.deadline_ms
    if config.task_timeout_ms is not None:
        effective_timeout_ms = min(effective_timeout_ms, config.task_timeout_ms)
    started = time.perf_counter()
    deadline = started + effective_timeout_ms / 1000.0
    try:
        business = task_function(chain, deadline)
    except TaskTimeout:
        result.update(
            {
                "status": "timeout",
                "conclusion": None,
                "errors": _task_error(
                    E_TIMEOUT,
                    "wall_clock_limit_exceeded",
                    f"task exceeded {effective_timeout_ms}ms wall-clock limit",
                ),
                "invalidations": [],
                "basis": None,
                "actual_summary": None,
                "matches_expected": False,
            }
        )
        return result
    except Exception as exc:  # A single bad task must not stop the batch.
        result.update(
            {
                "status": "rejected",
                "conclusion": None,
                "errors": _task_error(
                    E_INTERNAL,
                    "internal_task_error",
                    f"task raised {exc.__class__.__name__}",
                ),
                "invalidations": [],
                "basis": None,
                "actual_summary": None,
                "matches_expected": False,
            }
        )
        return result

    elapsed = time.perf_counter() - started
    if elapsed > effective_timeout_ms / 1000.0:
        result.update(
            {
                "status": "timeout",
                "conclusion": None,
                "errors": _task_error(
                    E_TIMEOUT,
                    "wall_clock_limit_exceeded",
                    f"task exceeded {effective_timeout_ms}ms wall-clock limit",
                ),
                "invalidations": [],
                "basis": None,
                "actual_summary": None,
                "matches_expected": False,
            }
        )
        return result

    result.update({"status": "success", **business})
    return result


def run_scheduled(
    chains: Iterable[ChainDeclaration],
    task_function: TaskFunction,
    config: SchedulerConfig,
    *,
    resume_results: dict[str, dict] | None = None,
    progress_callback: ProgressCallback | None = None,
    task_observer: TaskObserver | None = None,
) -> SchedulerOutcome:
    """Run tasks under deterministic admission and bounded parallelism.

    Queue capacity is a deterministic batch admission cap over the stable
    chain-id order, rather than a runtime occupancy counter. This is deliberate:
    it makes queue rejections independent of worker completion order.
    """

    ordered = sorted(chains, key=lambda item: item.sort_key)
    admitted = ordered[: config.queue_limit]
    queue_rejected = ordered[config.queue_limit :]
    runnable = admitted[: config.max_tasks]
    budget_rejected = admitted[config.max_tasks :]
    resume_results = dict(resume_results or {})
    results: dict[str, dict] = {}

    for chain in queue_rejected:
        result = _base_result(chain)
        result.update(
            {
                "status": "rejected",
                "conclusion": None,
                "errors": _task_error(
                    E_QUEUE_FULL,
                    "queue_capacity_exceeded",
                    f"queue admission limit is {config.queue_limit}",
                ),
                "invalidations": [],
                "basis": None,
                "actual_summary": None,
                "matches_expected": False,
            }
        )
        results[chain.chain_id] = result

    for chain in budget_rejected:
        result = _base_result(chain)
        result.update(
            {
                "status": "budget",
                "conclusion": None,
                "errors": _task_error(
                    E_BUDGET,
                    "task_budget_exceeded",
                    f"cumulative task budget is {config.max_tasks}",
                ),
                "invalidations": [],
                "basis": None,
                "actual_summary": None,
                "matches_expected": False,
            }
        )
        results[chain.chain_id] = result

    pending: list[ChainDeclaration] = []
    reused: list[str] = []
    for chain in runnable:
        stored = resume_results.get(chain.chain_id)
        if stored is not None:
            results[chain.chain_id] = stored
            reused.append(chain.chain_id)
        else:
            pending.append(chain)

    if progress_callback is not None:
        progress_callback(dict(results))

    executed: list[str] = []
    if pending:
        worker_count = min(config.workers, len(pending))
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="trustman"
        ) as executor:
            future_to_chain = {
                executor.submit(_run_task, chain, task_function, config): chain
                for chain in pending
            }
            for future in as_completed(future_to_chain):
                chain = future_to_chain[future]
                result = future.result()
                results[chain.chain_id] = result
                executed.append(chain.chain_id)
                if progress_callback is not None:
                    progress_callback(dict(results))
                if task_observer is not None:
                    task_observer(result)

    return SchedulerOutcome(
        results=results,
        executed_chain_ids=sorted(executed),
        reused_chain_ids=sorted(reused),
    )
