from __future__ import annotations

import hashlib
import time

from smuggle_detector.errors import DeadlineExceeded
from smuggle_detector.scheduler import SchedulerConfig, run_batch


def ok_worker(connection_id: str, timeout_ms: int) -> dict:
    return {
        "connection_id": connection_id,
        "status": "ok",
        "hits": [],
        "errors": [],
        "duplicate_count": 0,
    }


def test_queue_limit_is_stable_and_rejected_tasks_are_reported() -> None:
    results, summary, scheduling = run_batch(
        [f"conn-{index}" for index in range(6)],
        existing_results={},
        config=SchedulerConfig(
            concurrency=2,
            connection_timeout_ms=1000,
            batch_budget_ms=5000,
            queue_limit=1,
        ),
        worker=ok_worker,
    )
    assert scheduling["admission_capacity"] == 3
    assert [item["connection_id"] for item in results] == [
        f"conn-{index}" for index in range(6)
    ]
    assert summary["task_count"] == 6
    assert summary["success_count"] == 3
    assert summary["queue_rejected_count"] == 3
    assert [
        item["connection_id"]
        for item in results
        if item["status"] == "queue_full"
    ] == ["conn-3", "conn-4", "conn-5"]
    assert all(
        item["errors"][0]["code"] == "E_QUEUE_FULL"
        for item in results
        if item["status"] == "queue_full"
    )


def test_exhausted_budget_stops_admission_by_stable_key() -> None:
    started: list[str] = []

    def worker(connection_id: str, timeout_ms: int) -> dict:
        started.append(connection_id)
        return ok_worker(connection_id, timeout_ms)

    results, summary, scheduling = run_batch(
        ["c", "a", "b"],
        existing_results={},
        config=SchedulerConfig(
            concurrency=2,
            connection_timeout_ms=1000,
            batch_budget_ms=0,
            queue_limit=10,
        ),
        worker=worker,
    )
    assert started == []
    assert [item["connection_id"] for item in results] == ["a", "b", "c"]
    assert all(item["status"] == "budget_stopped" for item in results)
    assert summary["budget_stopped_count"] == 3
    assert scheduling["budget_stopped"] is True


def test_timeout_is_measured_by_real_wall_clock() -> None:
    def slow_worker(connection_id: str, timeout_ms: int) -> dict:
        deadline = time.perf_counter() + timeout_ms / 1000.0
        digest = hashlib.sha256()
        while time.perf_counter() < deadline:
            digest.update(b"real-cpu-work" * 256)
        raise DeadlineExceeded("deadline reached")

    started = time.perf_counter()
    results, summary, _ = run_batch(
        ["slow-a", "slow-b"],
        existing_results={},
        config=SchedulerConfig(
            concurrency=2,
            connection_timeout_ms=5,
            batch_budget_ms=1000,
            queue_limit=0,
        ),
        worker=slow_worker,
    )
    elapsed = time.perf_counter() - started
    assert elapsed >= 0.005
    assert summary["timeout_count"] == 2
    assert all(item["status"] == "timeout" for item in results)
    assert all(item["errors"][0]["code"] == "E_TIMEOUT" for item in results)


def test_worker_exception_is_isolated_to_one_connection() -> None:
    def worker(connection_id: str, timeout_ms: int) -> dict:
        if connection_id == "bad":
            raise ValueError("boom")
        return ok_worker(connection_id, timeout_ms)

    results, summary, _ = run_batch(
        ["good", "bad"],
        existing_results={},
        config=SchedulerConfig(
            concurrency=2,
            connection_timeout_ms=1000,
            batch_budget_ms=1000,
            queue_limit=0,
        ),
        worker=worker,
    )
    by_id = {item["connection_id"]: item for item in results}
    assert by_id["good"]["status"] == "ok"
    assert by_id["bad"]["status"] == "failed"
    assert summary["failure_count"] == 1
    assert summary["success_count"] == 1

