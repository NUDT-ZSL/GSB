"""Deterministic serial/process-pool execution with structured task failures."""

from __future__ import annotations

import copy
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import numpy as np

from .errors import EngineError, coerce_engine_error
from .methods import score_isolation_forest, score_knn, score_mahalanobis

_WORKER_DATA: dict[str, Any] | None = None


def _worker_init(payload: dict[str, Any]) -> None:
    global _WORKER_DATA
    _WORKER_DATA = payload


def _score_task(task: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    data = np.asarray(payload["features"], dtype=np.float64)
    sample_ids = tuple(payload["sample_ids"])
    common = {
        "sample_ids": sample_ids,
        "data": data,
        "params": task["params"],
        "seed": int(task["seed"]),
        "candidate_id": task["candidate_id"],
        "row_chunk_size": int(task["row_chunk_size"]),
    }
    method = task["method"]
    if method == "isolation_forest":
        return score_isolation_forest(**common)
    if method == "knn":
        return score_knn(**common)
    if method == "mahalanobis":
        return score_mahalanobis(**common)
    raise EngineError(
        "E_UNKNOWN_METHOD",
        "worker_dispatch",
        f"unknown method {method!r}",
        field="method",
        method=method,
    )


def _worker_score(task: dict[str, Any]) -> dict[str, Any]:
    if _WORKER_DATA is None:
        return {
            "status": "error",
            "task_id": task["task_id"],
            "error": EngineError(
                "E_WORKER_NOT_INITIALIZED",
                "worker_init",
                "worker dataset was not initialized",
                method=task.get("method"),
                batch_index=task.get("batch_index"),
                task_id=task.get("task_id"),
            ).to_dict(),
        }
    try:
        result = _score_task(task, _WORKER_DATA)
        return {"status": "ok", "task_id": task["task_id"], "result": result}
    except Exception as exc:
        error = coerce_engine_error(
            exc,
            stage="worker_execution",
            code="E_WORKER_EXCEPTION",
            method=task.get("method"),
            batch_index=task.get("batch_index"),
            task_id=task.get("task_id"),
        )
        return {"status": "error", "task_id": task["task_id"], "error": error.to_dict()}


def _failure_envelope(task: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    error = coerce_engine_error(
        exc,
        stage="process_pool",
        code="E_WORKER_PROCESS",
        method=task.get("method"),
        batch_index=task.get("batch_index"),
        task_id=task.get("task_id"),
    )
    return {"status": "error", "task_id": task["task_id"], "error": error.to_dict()}


def execute_tasks(
    *,
    tasks: list[dict[str, Any]],
    dataset_payload: dict[str, Any],
    mode: str,
    workers: int,
    start_method: str,
    batches: list[dict[str, Any]] | None = None,
    row_chunk_override: int | None = None,
) -> dict[str, Any]:
    prepared = copy.deepcopy(tasks)
    if row_chunk_override is not None:
        for task in prepared:
            task["row_chunk_size"] = max(1, min(int(row_chunk_override), len(dataset_payload["sample_ids"])))
    envelopes: list[dict[str, Any]] = []

    if mode == "serial":
        for task in prepared:
            envelopes.append(_worker_score_with_payload(task, dataset_payload))
    elif mode == "parallel":
        context = multiprocessing.get_context(start_method)
        with ProcessPoolExecutor(
            max_workers=workers,
            mp_context=context,
            initializer=_worker_init,
            initargs=(dataset_payload,),
        ) as executor:
            by_id = {task["task_id"]: task for task in prepared}
            batch_specs = batches or [
                {
                    "batch_index": 0,
                    "task_ids": [task["task_id"] for task in prepared],
                }
            ]
            for batch in batch_specs:
                batch_tasks = [by_id[task_id] for task_id in batch["task_ids"] if task_id in by_id]
                futures = [executor.submit(_worker_score, task) for task in batch_tasks]
                for task, future in zip(batch_tasks, futures):
                    try:
                        envelopes.append(future.result())
                    except Exception as exc:
                        envelopes.append(_failure_envelope(task, exc))
    else:
        raise EngineError(
            "E_INVALID_SCHEDULER_MODE",
            "scheduler",
            f"unknown scheduler mode {mode!r}",
            field="mode",
        )

    order = {task["task_id"]: index for index, task in enumerate(prepared)}
    envelopes.sort(key=lambda envelope: order.get(envelope["task_id"], len(order)))
    successful = [envelope["task_id"] for envelope in envelopes if envelope["status"] == "ok"]
    failed = [envelope["task_id"] for envelope in envelopes if envelope["status"] != "ok"]
    return {
        "mode": mode,
        "workers": workers,
        "start_method": start_method if mode == "parallel" else None,
        "row_chunk_override": row_chunk_override,
        "dispatch_order": [task["task_id"] for task in prepared],
        "envelopes": envelopes,
        "successful_task_ids": successful,
        "failed_task_ids": failed,
    }


def _worker_score_with_payload(
    task: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        return {"status": "ok", "task_id": task["task_id"], "result": _score_task(task, payload)}
    except Exception as exc:
        error = coerce_engine_error(
            exc,
            stage="serial_execution" if task.get("mode") != "worker" else "worker_execution",
            code="E_WORKER_EXCEPTION",
            method=task.get("method"),
            batch_index=task.get("batch_index"),
            task_id=task.get("task_id"),
        )
        return {"status": "error", "task_id": task["task_id"], "error": error.to_dict()}
