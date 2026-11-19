"""Local spawn-based process pool execution with deterministic chunk merging."""

from __future__ import annotations

import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Iterable

from .compute import error_result, run_job
from .errors import E_EXECUTOR_UNAVAILABLE, E_WORKER_CRASH, BusinessError
from .models import PipelineConfig, Task


def worker_entry(payload: dict[str, Any]) -> dict[str, Any]:
    """Top-level picklable worker entry point (required by Windows spawn)."""

    task = Task.from_dict(payload["task"])
    config = PipelineConfig.from_dict(payload["config"])
    return run_job(task, config)


def serial_run(tasks: Iterable[Task], config: PipelineConfig) -> list[dict[str, Any]]:
    """Reference execution order: ascending job_id, one task at a time."""

    return [run_job(task, config) for task in sorted(tasks, key=lambda item: item.job_id)]


class SpawnExecutor:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def _payload(self, task: Task) -> dict[str, Any]:
        return {
            "task": task.to_dict(),
            "config": self.config.to_dict(),
        }

    def run(self, plan: dict[str, Any]) -> dict[str, Any]:
        admitted: dict[str, Task] = {}
        for decision in plan["decisions"]:
            if decision["decision"] == "admitted":
                admitted[decision["job_id"]] = Task.from_dict(
                    decision["effective_task"]
                )

        if not admitted:
            return {
                "results": [],
                "execution_chunks": [],
                "merge_order": [],
                "pool_mode": "spawn",
                "worker_count": 0,
            }

        worker_count = max(1, min(self.config.max_slots, len(admitted)))
        results: list[dict[str, Any]] = []
        execution_chunks: list[dict[str, Any]] = []
        merge_order: list[str] = []

        try:
            context = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(
                max_workers=worker_count,
                mp_context=context,
            ) as pool:
                for group in plan["parallel_groups"]:
                    group_ids = list(group["job_ids"])
                    if not group_ids:
                        continue
                    future_to_task = {
                        pool.submit(
                            worker_entry,
                            self._payload(admitted[job_id]),
                        ): admitted[job_id]
                        for job_id in group_ids
                    }
                    by_job_id: dict[str, dict[str, Any]] = {}
                    for future in as_completed(future_to_task):
                        task = future_to_task[future]
                        try:
                            by_job_id[task.job_id] = future.result()
                        except Exception as exc:
                            # A process/infrastructure failure is attributed to
                            # this job only; no retry and no duplicate result.
                            by_job_id[task.job_id] = error_result(
                                task,
                                E_WORKER_CRASH,
                                "worker_execute",
                                f"{type(exc).__name__}: {exc}",
                                {"job_id": task.job_id},
                            )
                    for job_id in group_ids:
                        results.append(by_job_id[job_id])
                        merge_order.append(job_id)
                    execution_chunks.append(
                        {
                            "group_index": group["group_index"],
                            "job_ids": group_ids,
                            "result_job_ids": group_ids,
                        }
                    )
        except BusinessError:
            raise
        except Exception as exc:
            raise BusinessError(
                E_EXECUTOR_UNAVAILABLE,
                "local spawn process pool is unavailable",
                stage="executor_start",
                details={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc

        return {
            "results": results,
            "execution_chunks": execution_chunks,
            "merge_order": merge_order,
            "pool_mode": "spawn",
            "worker_count": worker_count,
        }
