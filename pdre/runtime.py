"""Bounded concurrent unit processing."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Callable

from .codes import BusinessCode as Codes
from .config import EngineConfig
from .judgment import JudgeOutcome, judge_unit
from .lineage import Assignment
from .models import EventRecord, PolicyRecord


_SENTINEL = object()


@dataclass(frozen=True)
class UnitJob:
    assignment: Assignment
    events: list[EventRecord]
    policies: list[PolicyRecord]

    @property
    def unit_id(self) -> str:
        return self.assignment.unit_id


class BoundedUnitProcessor:
    def __init__(
        self,
        config: EngineConfig,
        handler: Callable[[Assignment, list[EventRecord], list[PolicyRecord], EngineConfig], JudgeOutcome] = judge_unit,
    ) -> None:
        self.config = config
        self.handler = handler
        self._queue: queue.Queue[object] = queue.Queue(maxsize=config.max_queue_size)
        self._workers: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.units: list[dict] = []
        self.timings_ms: dict[str, float] = {}
        self.queue_rejections: list[dict] = []

    def start(self) -> "BoundedUnitProcessor":
        for index in range(self.config.concurrency):
            thread = threading.Thread(
                target=self._worker,
                name=f"pdre-unit-worker-{index}",
                daemon=True,
            )
            thread.start()
            self._workers.append(thread)
        return self

    def __enter__(self) -> "BoundedUnitProcessor":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.wait()

    def submit(self, job: UnitJob) -> bool:
        try:
            self._queue.put_nowait(job)
            return True
        except queue.Full:
            with self._lock:
                self.queue_rejections.append(
                    {
                        "code": Codes.QUEUE_FULL,
                        "unit_id": job.unit_id,
                        "message": (
                            "pending unit queue is full; unit was rejected before enqueue"
                        ),
                        "max_queue_size": self.config.max_queue_size,
                    }
                )
            return False

    def wait(self) -> None:
        for _ in self._workers:
            self._queue.put(_SENTINEL)
        for worker in self._workers:
            worker.join()
        self.units.sort(key=lambda unit: unit["unit_id"])

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _SENTINEL:
                    return
                job = item
                assert isinstance(job, UnitJob)
                outcome = self.handler(
                    job.assignment, job.events, job.policies, self.config
                )
                with self._lock:
                    self.units.append(outcome.unit)
                    self.timings_ms[job.unit_id] = outcome.elapsed_ms
            finally:
                self._queue.task_done()


def run_jobs(
    jobs: list[UnitJob],
    config: EngineConfig,
    handler: Callable[..., JudgeOutcome] = judge_unit,
) -> tuple[list[dict], list[dict], dict[str, float]]:
    processor = BoundedUnitProcessor(config, handler=handler)
    with processor:
        for job in jobs:
            processor.submit(job)
    return processor.units, processor.queue_rejections, processor.timings_ms
