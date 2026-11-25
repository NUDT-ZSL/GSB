"""Deterministic admission control, chunking, and process-pool workers."""

from __future__ import annotations

import json
import os
from multiprocessing import get_context
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from .arbitration import estimate_request
from .formats import Repository
from .merge import merge_request
from .planning import Plan, sorted_requests
from .retrieval import retrieve


BUDGET_CODES = {
    "concurrency": "E_CONCURRENCY_BUDGET",
    "time": "E_TIME_BUDGET",
    "memory": "E_MEMORY_BUDGET",
}


def admit_requests(
    repository: Repository,
    requests: list[dict[str, Any]],
    plan: Plan,
    config: dict[str, Any],
) -> dict[str, Any]:
    scheduling = config["scheduling"]
    precedence = scheduling.get(
        "budget_precedence", ["concurrency", "time", "memory"]
    )
    concurrency_limit = int(scheduling["concurrency_limit"])
    time_budget = float(scheduling["time_budget_ms"])
    memory_budget = int(scheduling["memory_budget_bytes"])
    block_size = max(1, int(config["precision"].get("block_size", concurrency_limit)))
    effective_workers = min(
        concurrency_limit,
        int(
            config["scheduling"].get(
                "effective_concurrency", concurrency_limit
            )
        ),
    )
    chunk_size = min(block_size, max(1, effective_workers))
    blocking_post_admission = {
        "E_FORMAT_CORRUPT",
        "E_VERSION_UNSUPPORTED",
    }
    ordered = sorted_requests(requests)
    elapsed_ms = 0.0
    resident_refs: set[str] = set()
    resident_bytes = 0
    admitted: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for request in ordered:
        estimate = estimate_request(repository, request, config)
        violations: dict[str, Any] = {}
        if request["required_lanes"] > concurrency_limit:
            violations["concurrency"] = {
                "required_lanes": request["required_lanes"],
                "limit": concurrency_limit,
            }
        if (
            estimate["time_ms"] > time_budget
            or elapsed_ms + estimate["time_ms"] > time_budget
        ):
            violations["time"] = {
                "request_time_ms": estimate["time_ms"],
                "elapsed_before_ms": round(elapsed_ms, 8),
                "limit_ms": time_budget,
            }
        new_refs = [
            result_ref
            for result_ref in estimate["unique_result_refs"]
            if result_ref not in resident_refs
        ]
        new_bytes = sum(
            len(
                (
                    repository.entries[result_ref]["content"].encode("utf-8")
                    if isinstance(
                        repository.entries[result_ref]["content"], str
                    )
                    else json.dumps(
                        repository.entries[result_ref]["content"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            )
            for result_ref in new_refs
        )
        if resident_bytes + new_bytes > memory_budget:
            violations["memory"] = {
                "resident_bytes": resident_bytes,
                "incremental_bytes": new_bytes,
                "limit_bytes": memory_budget,
            }
        precheck_error = None
        for call in request["tool_calls"]:
            if call["result_ref"] not in repository.entries:
                precheck_error = {
                    "code": "E_FORMAT_CORRUPT",
                    "message": "referenced tool result is missing",
                    "details": {"result_ref": call["result_ref"]},
                }
                break
        primary = next(
            (axis for axis in precedence if axis in violations), None
        )
        if precheck_error is not None:
            code = precheck_error["code"]
            admitted_here = False
            reason = precheck_error["message"]
        elif primary is not None:
            code = BUDGET_CODES[primary]
            admitted_here = False
            reason = f"{primary} budget admission rejected"
        else:
            code = "E_OK"
            admitted_here = True
            reason = "admitted"
        decision = {
            "unit_id": request["unit_id"],
            "request_id": request["request_id"],
            "turn_no": request["turn_no"],
            "admitted": admitted_here,
            "business_code": code,
            "reason": reason,
            "precheck_error": precheck_error,
            "primary_budget": primary,
            "violations": violations,
            "estimate": estimate,
            "required_lanes": request["required_lanes"],
            "elapsed_before_ms": round(elapsed_ms, 8),
            "remaining_time_ms": round(time_budget - elapsed_ms, 8),
            "resident_before_bytes": resident_bytes,
            "resident_limit_bytes": memory_budget,
        }
        decisions.append(decision)
        if not admitted_here:
            continue
        elapsed_ms += estimate["time_ms"]
        resident_refs.update(estimate["unique_result_refs"])
        resident_bytes += new_bytes
        admitted.append(
            {
                "request": request,
                "estimate": estimate,
                "plan_nodes": plan.request_nodes[request["unit_id"]],
            }
        )
    chunks = [
        admitted[index : index + chunk_size]
        for index in range(0, len(admitted), chunk_size)
    ]
    return {
        "limits": {
            "concurrency_limit": concurrency_limit,
            "effective_concurrency": effective_workers,
            "time_budget_ms": time_budget,
            "memory_budget_bytes": memory_budget,
            "block_size": block_size,
            "chunk_size": chunk_size,
            "budget_precedence": precedence,
        },
        "admitted_units": [item["request"]["unit_id"] for item in admitted],
        "decisions": decisions,
        "chunks": [
            [item["request"]["unit_id"] for item in chunk] for chunk in chunks
        ],
        "resident_bytes_final": resident_bytes,
        "logical_time_used_ms": round(elapsed_ms, 8),
        "admitted": admitted,
    }


_WORKER_CONTEXT: dict[str, Any] = {}


def _init_worker(context: dict[str, Any]) -> None:
    global _WORKER_CONTEXT
    _WORKER_CONTEXT = context


def _worker_task(task: dict[str, Any]) -> dict[str, Any]:
    try:
        retrieval_artifact = task.get("retrieval")
        retrieval_computed = False
        if retrieval_artifact is None:
            retrieval_artifact = retrieve(
                task["request"]["query_text"],
                _WORKER_CONTEXT["config"],
                _WORKER_CONTEXT["index"],
                task.get("context_text", ""),
            )
            retrieval_computed = True
        merge_artifact = merge_request(
            task["request"],
            task["parsed_results"],
            retrieval_artifact,
            _WORKER_CONTEXT["corpus"],
            _WORKER_CONTEXT["config"],
            task["merge_key"],
            task["retrieval_key"],
            task["read_keys"],
        )
        return {
            "ok": True,
            "unit_id": task["request"]["unit_id"],
            "retrieval": retrieval_artifact,
            "retrieval_computed": retrieval_computed,
            "merge": merge_artifact,
        }
    except Exception as exc:  # Worker exceptions are serialized as data.
        from .errors import WorkbenchError

        if isinstance(exc, WorkbenchError):
            return {
                "ok": False,
                "unit_id": task.get("request", {}).get("unit_id"),
                "error": exc.to_dict(),
            }
        return {
            "ok": False,
            "unit_id": task.get("request", {}).get("unit_id"),
            "error": {
                "code": "E_INCONSISTENT",
                "message": "unexpected worker failure",
                "details": {"error": repr(exc)},
            },
        }


def run_tasks_in_chunks(
    tasks_by_unit: dict[str, dict[str, Any]],
    chunks: list[list[str]],
    concurrency: int,
) -> tuple[dict[str, dict[str, Any]], list[list[str]]]:
    """Run each deterministic chunk in order using a local process pool."""

    results: dict[str, dict[str, Any]] = {}
    executed_chunks: list[list[str]] = []
    pending = [
        unit for chunk in chunks for unit in chunk if unit in tasks_by_unit
    ]
    if not pending:
        return results, executed_chunks
    context = get_context("fork" if os.name == "posix" else "spawn")
    with ProcessPoolExecutor(
        max_workers=max(1, concurrency),
        mp_context=context,
        initializer=_init_worker,
        initargs=(_WORKER_CONTEXT,),
    ) as executor:
        for chunk in chunks:
            chunk_tasks = [
                tasks_by_unit[unit] for unit in chunk if unit in tasks_by_unit
            ]
            if not chunk_tasks:
                continue
            executed_chunks.append([task["request"]["unit_id"] for task in chunk_tasks])
            for result in executor.map(_worker_task, chunk_tasks):
                results[result["unit_id"]] = result
    return results, executed_chunks


def set_worker_context(context: dict[str, Any]) -> None:
    global _WORKER_CONTEXT
    _WORKER_CONTEXT = context

