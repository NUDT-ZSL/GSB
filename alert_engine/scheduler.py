"""Bounded host-level scheduler with wall-clock and budget enforcement."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .clustering import HostClusterResult, cluster_host
from .codes import Code
from .errors import HostTimeout
from .models import Alert, BoundAlert, EngineConfig, SchedulerReport


@dataclass(frozen=True)
class HostJob:
    host_id: str
    all_alerts: tuple[Alert, ...]
    bound_alerts: tuple[BoundAlert, ...]
    total_alert_count: int
    include_cycles: bool = True


HostWorker = Callable[..., HostClusterResult]


@dataclass
class _HostOutcome:
    host_id: str
    result: HostClusterResult | None
    issue: dict | None
    elapsed_ms: float


def run_hosts(
    jobs: list[HostJob],
    config: EngineConfig,
    *,
    worker: HostWorker = cluster_host,
) -> tuple[dict[str, HostClusterResult], SchedulerReport]:
    config.validate()
    report = SchedulerReport(hosts=len(jobs))
    if not jobs:
        return {}, report

    ordered_jobs = sorted(jobs, key=lambda item: item.host_id)
    pending: queue.Queue[HostJob | object] = queue.Queue(
        maxsize=config.pending_host_queue_limit
    )
    outcomes: queue.Queue[_HostOutcome] = queue.Queue()
    sentinel = object()
    started_at = time.perf_counter()

    def worker_loop() -> None:
        while True:
            item = pending.get()
            if item is sentinel:
                pending.task_done()
                return
            assert isinstance(item, HostJob)
            processing_started = time.perf_counter()
            try:
                if item.total_alert_count > config.max_alerts_per_host:
                    issue = {
                        "code": Code.E_BUDGET,
                        "host_id": item.host_id,
                        "alert_count": item.total_alert_count,
                        "limit": config.max_alerts_per_host,
                        "message": "host alert budget exceeded",
                    }
                    outcomes.put(
                        _HostOutcome(
                            item.host_id,
                            None,
                            issue,
                            (time.perf_counter() - processing_started) * 1000.0,
                        )
                    )
                    continue
                deadline = (
                    processing_started
                    + config.per_host_timeout_ms / 1000.0
                )
                result = worker(
                    host_id=item.host_id,
                    all_alerts=list(item.all_alerts),
                    bound_alerts=list(item.bound_alerts),
                    deadline=deadline,
                    include_cycles=item.include_cycles,
                )
                outcomes.put(
                    _HostOutcome(
                        item.host_id,
                        result,
                        None,
                        (time.perf_counter() - processing_started) * 1000.0,
                    )
                )
            except HostTimeout:
                outcomes.put(
                    _HostOutcome(
                        item.host_id,
                        None,
                        {
                            "code": Code.E_TIMEOUT,
                            "host_id": item.host_id,
                            "timeout_ms": config.per_host_timeout_ms,
                            "message": (
                                "host processing exceeded its wall-clock budget"
                            ),
                        },
                        (time.perf_counter() - processing_started) * 1000.0,
                    )
                )
            except Exception as exc:
                outcomes.put(
                    _HostOutcome(
                        item.host_id,
                        None,
                        {
                            "code": Code.E_INTERNAL,
                            "host_id": item.host_id,
                            "message": str(exc),
                            "exception_type": type(exc).__name__,
                        },
                        (time.perf_counter() - processing_started) * 1000.0,
                    )
                )
            finally:
                pending.task_done()

    threads = [
        threading.Thread(
            target=worker_loop,
            name=f"alert-host-worker-{index}",
            daemon=True,
        )
        for index in range(config.max_workers)
    ]
    for thread in threads:
        thread.start()

    accepted = 0
    admission_capacity = (
        config.max_workers + config.pending_host_queue_limit
    )
    for position, job in enumerate(ordered_jobs):
        if position >= admission_capacity:
            report.issues.append(
                {
                    "code": Code.E_QUEUE_FULL,
                    "host_id": job.host_id,
                    "queue_limit": config.pending_host_queue_limit,
                    "admission_capacity": admission_capacity,
                    "message": (
                        "host was rejected because the bounded admission "
                        "window is full"
                    ),
                }
            )
            report.rejected_hosts += 1
            continue
        # The capacity check above makes the admission set deterministic.
        # Blocking put only applies inside that window and lets workers drain
        # the bounded queue without a timing-dependent accept/reject race.
        pending.put(job)
        accepted += 1

    for _ in threads:
        pending.put(sentinel)
    for thread in threads:
        thread.join()

    results: dict[str, HostClusterResult] = {}
    for _ in range(accepted):
        outcome = outcomes.get()
        report.host_elapsed_ms[outcome.host_id] = round(outcome.elapsed_ms, 6)
        if outcome.issue is not None:
            report.issues.append(outcome.issue)
            report.rejected_hosts += 1
            continue
        assert outcome.result is not None
        results[outcome.host_id] = outcome.result
        report.processed_hosts += 1

    report.elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    report.issues.sort(
        key=lambda item: (
            item.get("host_id", ""),
            item.get("code", ""),
            item.get("message", ""),
        )
    )
    return results, report
