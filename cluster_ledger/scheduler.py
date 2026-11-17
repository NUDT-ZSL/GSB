"""Quota-safe parallel dispatch scheduler."""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .client import DispatchClient, DispatchOutcome
from .clock import CLIENT_CLOCK_BASIS, mono_ms, mono_ns
from .config import ClusterSpec, Declaration, TaskSpec
from .errors import E_SERVICE, BusinessError
from .ledger import Ledger
from .logging_utils import EventLogger


@dataclass(frozen=True)
class DispatchUnit:
    task: TaskSpec
    cluster: ClusterSpec
    queued_mono_ms: int


@dataclass
class UnitOutcome:
    unit: DispatchUnit
    ok: bool
    outcome: DispatchOutcome | None = None
    error: BusinessError | None = None
    started_mono_ms: int = 0
    finished_mono_ms: int = 0


@dataclass
class ScheduleReport:
    outcomes: list[UnitOutcome] = field(default_factory=list)
    observed_peak_global: int = 0
    observed_peak_by_cluster: dict[str, int] = field(default_factory=dict)
    global_limit: int = 0
    per_cluster_limits: dict[str, int] = field(default_factory=dict)
    started_mono_ms: int = 0
    finished_mono_ms: int = 0

    @property
    def successful(self) -> int:
        return sum(1 for item in self.outcomes if item.ok)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.outcomes if not item.ok)

    def as_dict(self) -> dict[str, Any]:
        return {
            "global_limit": self.global_limit,
            "per_cluster_limits": self.per_cluster_limits,
            "observed_peak_global": self.observed_peak_global,
            "observed_peak_by_cluster": self.observed_peak_by_cluster,
            "successful": self.successful,
            "failed": self.failed,
            "started_mono_ms": self.started_mono_ms,
            "finished_mono_ms": self.finished_mono_ms,
            "outcomes": [
                {
                    "task_id": item.unit.task.task_id,
                    "cluster_id": item.unit.cluster.cluster_id,
                    "config_version": item.unit.cluster.config_version,
                    "ok": item.ok,
                    "attempts": item.outcome.attempts if item.outcome else 0,
                    "summary": item.outcome.response.get("summary") if item.outcome else None,
                    "error": item.error.to_dict() if item.error else None,
                }
                for item in self.outcomes
            ],
        }


class DispatchScheduler:
    def __init__(self, declaration: Declaration, ledger: Ledger, *,
                 client: DispatchClient | None = None,
                 logger: EventLogger | None = None,
                 on_observation: Callable[[str, DispatchUnit, int, dict[str, int]], None] | None = None) -> None:
        self.declaration = declaration
        self.ledger = ledger
        self.client = client or DispatchClient()
        self.logger = logger or EventLogger(None)
        self.on_observation = on_observation
        self._condition = threading.Condition()
        self._active = 0
        self._active_by_cluster: dict[str, int] = {}
        self._peak = 0
        self._peak_by_cluster: dict[str, int] = {}

    def _per_cluster_limit(self, cluster: ClusterSpec) -> int:
        configured = cluster.per_cluster_concurrency or self.declaration.limits.per_cluster_concurrency
        return max(1, min(configured, self.declaration.limits.global_concurrency))

    def _eligible(self, pending: deque[DispatchUnit]) -> int | None:
        for index, unit in enumerate(pending):
            if self._active >= self.declaration.limits.global_concurrency:
                return None
            cluster_active = self._active_by_cluster.get(unit.cluster.cluster_id, 0)
            if cluster_active < self._per_cluster_limit(unit.cluster):
                return index
        return None

    def _record_dispatch_interaction(self, unit: DispatchUnit, *, ok: bool,
                                     started_ms: int, finished_ms: int,
                                     started_ns: int, finished_ns: int,
                                     outcome: DispatchOutcome | None,
                                     error: BusinessError | None) -> None:
        detail: dict[str, Any] = {
            "aggregation_mode": unit.task.aggregation_mode,
            "timeout_ms": unit.task.timeout_ms,
            "retries": unit.task.retries,
            "queue_wait_mono_ms": max(0, started_ms - unit.queued_mono_ms),
        }
        if outcome is not None:
            detail.update(
                {
                    "attempts": outcome.attempts,
                    "calculation_duration_ms": outcome.response.get("calculation_duration_ms"),
                    "server_started_mono_ms": outcome.response.get("server_started_mono_ms"),
                    "server_finished_mono_ms": outcome.response.get("server_finished_mono_ms"),
                    "attempt_errors": list(outcome.attempt_errors),
                    "client_clock_basis": CLIENT_CLOCK_BASIS,
                }
            )
        if error is not None:
            detail["error"] = error.to_dict()
        self.ledger.record_interaction(
            kind="dispatch",
            task_id=unit.task.task_id,
            cluster_id=unit.cluster.cluster_id,
            config_version=unit.cluster.config_version,
            started_mono_ms=started_ms,
            ended_mono_ms=finished_ms,
            started_mono_ns=started_ns,
            ended_mono_ns=finished_ns,
            data_version=f"{self.declaration.declaration_version}:v{unit.cluster.config_version}",
            status="ok" if ok else "error",
            business_code=error.code if error else None,
            detail=detail,
        )

    def _run_unit(self, unit: DispatchUnit) -> UnitOutcome:
        started_ns = mono_ns()
        started_ms = started_ns // 1_000_000
        try:
            outcome = self.client.dispatch(unit.task, unit.cluster)
            response = outcome.response
            if int(response.get("config_version", -1)) != unit.cluster.config_version:
                raise BusinessError(
                    E_SERVICE,
                    "service returned a different config version",
                    details={
                        "expected": unit.cluster.config_version,
                        "actual": response.get("config_version"),
                    },
                )
            self.ledger.record_result(
                task_id=unit.task.task_id,
                cluster_id=unit.cluster.cluster_id,
                config_version=unit.cluster.config_version,
                aggregation_mode=str(response.get("aggregation_mode", unit.task.aggregation_mode)),
                summary=str(response["summary"]),
                components=response["components"],
                entry_count=int(response.get("entry_count", 0)),
                calculation_duration_ms=int(response["calculation_duration_ms"]),
                dispatch_started_mono_ms=outcome.started_mono_ms,
                dispatch_finished_mono_ms=outcome.finished_mono_ms,
                dispatch_started_mono_ns=outcome.started_mono_ns,
                dispatch_finished_mono_ns=outcome.finished_mono_ns,
                declaration_version=self.declaration.declaration_version,
            )
            self._record_dispatch_interaction(
                unit,
                ok=True,
                started_ms=outcome.started_mono_ms,
                finished_ms=outcome.finished_mono_ms,
                started_ns=outcome.started_mono_ns,
                finished_ns=outcome.finished_mono_ns,
                outcome=outcome,
                error=None,
            )
            self.logger.emit(
                "dispatch_completed",
                task_id=unit.task.task_id,
                cluster_id=unit.cluster.cluster_id,
                config_version=unit.cluster.config_version,
                attempts=outcome.attempts,
                summary=response["summary"],
                started_mono_ms=outcome.started_mono_ms,
                finished_mono_ms=outcome.finished_mono_ms,
            )
            return UnitOutcome(
                unit=unit,
                ok=True,
                outcome=outcome,
                started_mono_ms=outcome.started_mono_ms,
                finished_mono_ms=outcome.finished_mono_ms,
            )
        except BusinessError as exc:
            error = exc
        except Exception as exc:  # defensive scheduler boundary
            error = BusinessError(
                E_SERVICE,
                f"unexpected dispatch failure: {type(exc).__name__}: {exc}",
                details={"task_id": unit.task.task_id, "cluster_id": unit.cluster.cluster_id},
            )
        finished_ns = mono_ns()
        finished_ms = finished_ns // 1_000_000
        self._record_dispatch_interaction(
            unit,
            ok=False,
            started_ms=started_ms,
            finished_ms=finished_ms,
            started_ns=started_ns,
            finished_ns=finished_ns,
            outcome=None,
            error=error,
        )
        self.ledger.mark_recovery_unknown(
            [(unit.task.task_id, unit.cluster.cluster_id, unit.cluster.config_version, error.code)]
        )
        self.logger.error(
            error,
            event="dispatch_failed",
            task_id=unit.task.task_id,
            cluster_id=unit.cluster.cluster_id,
            config_version=unit.cluster.config_version,
            started_mono_ms=started_ms,
            finished_mono_ms=finished_ms,
        )
        return UnitOutcome(
            unit=unit,
            ok=False,
            error=error,
            started_mono_ms=started_ms,
            finished_mono_ms=finished_ms,
        )

    def run(self, *, task_ids: Iterable[str] | None = None,
            cluster_ids: Iterable[str] | None = None) -> ScheduleReport:
        # Reconcile defensively even when called as a library rather than
        # through the CLI, so version rollback and stale epochs are enforced
        # before any request leaves the process.
        self.ledger.reconcile(self.declaration)
        task_filter = set(task_ids) if task_ids else None
        cluster_filter = set(cluster_ids) if cluster_ids else None
        clusters = self.declaration.cluster_map()
        now = mono_ms()
        units: list[DispatchUnit] = []
        for task in self.declaration.tasks:
            if task_filter is not None and task.task_id not in task_filter:
                continue
            for cluster_id in task.target_clusters:
                if cluster_filter is not None and cluster_id not in cluster_filter:
                    continue
                units.append(DispatchUnit(task=task, cluster=clusters[cluster_id], queued_mono_ms=now))

        report = ScheduleReport(
            global_limit=self.declaration.limits.global_concurrency,
            per_cluster_limits={cluster.cluster_id: self._per_cluster_limit(cluster) for cluster in self.declaration.clusters},
            started_mono_ms=now,
        )
        pending = deque(units)
        futures: dict[Future[UnitOutcome], DispatchUnit] = {}

        def finished_callback(future: Future[UnitOutcome], unit: DispatchUnit) -> None:
            with self._condition:
                self._active = max(0, self._active - 1)
                self._active_by_cluster[unit.cluster.cluster_id] = max(
                    0, self._active_by_cluster.get(unit.cluster.cluster_id, 1) - 1
                )
                self._condition.notify_all()

        with ThreadPoolExecutor(
            max_workers=self.declaration.limits.global_concurrency,
            thread_name_prefix="dispatch",
        ) as executor:
            while pending or futures:
                with self._condition:
                    while pending:
                        eligible = self._eligible(pending)
                        if eligible is None:
                            break
                        unit = pending[eligible]
                        del pending[eligible]
                        self._active += 1
                        cluster_id = unit.cluster.cluster_id
                        self._active_by_cluster[cluster_id] = self._active_by_cluster.get(cluster_id, 0) + 1
                        self._peak = max(self._peak, self._active)
                        self._peak_by_cluster[cluster_id] = max(
                            self._peak_by_cluster.get(cluster_id, 0),
                            self._active_by_cluster[cluster_id],
                        )
                        if self.on_observation:
                            self.on_observation(
                                "start", unit, self._active, dict(self._active_by_cluster)
                            )
                        future = executor.submit(self._run_unit, unit)
                        futures[future] = unit
                        future.add_done_callback(lambda fut, item=unit: finished_callback(fut, item))

                done = [future for future in futures if future.done()]
                for future in done:
                    unit = futures.pop(future)
                    report.outcomes.append(future.result())
                    if self.on_observation:
                        with self._condition:
                            self.on_observation(
                                "finish", unit, self._active, dict(self._active_by_cluster)
                            )
                if not done:
                    with self._condition:
                        self._condition.wait(timeout=0.02)

        report.observed_peak_global = self._peak
        report.observed_peak_by_cluster = dict(self._peak_by_cluster)
        report.finished_mono_ms = mono_ms()
        return report
