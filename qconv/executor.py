from __future__ import annotations

import hashlib
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from typing import Any

import numpy as np

from .canonical import checksum
from .errors import DomainError, ErrorCode
from .models import ExecutionConfig, Job, SchedulerConfig
from .quant_conv import compute_chunk, merge_chunks

EXECUTION_STAGES = (
    "prepare",
    "quantize_input",
    "fixed_mac",
    "accumulate_reduce",
    "dequantize_output",
)


def _failure(
    *,
    job_id: str,
    chunk_id: int,
    stage: str,
    code: ErrorCode | str,
    message: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "job_id": job_id,
        "chunk_id": chunk_id,
        "stage": stage,
        "code": code.value if isinstance(code, ErrorCode) else str(code),
        "message": message,
        "detail": detail or {},
    }


def execute_chunk_task(task: dict[str, Any]) -> dict[str, Any]:
    job_id = str(task.get("job_id", ""))
    chunk = task.get("chunk", {})
    chunk_id = int(chunk.get("chunk_id", -1))
    stage_hint = "prepare"
    try:
        job = Job.from_dict(task["job"])
        config = SchedulerConfig.from_dict(task.get("scheduler_config"))
        reduction_mode = str(task.get("reduction_mode", "int64_exact"))
        injections = task.get("fault_injections", [])
        for stage in EXECUTION_STAGES:
            for injection in injections:
                if injection.get("job_id") != job.job_id:
                    continue
                injected_chunk = injection.get("chunk_id")
                if injected_chunk is not None and int(injected_chunk) != chunk_id:
                    continue
                if injection.get("stage", "fixed_mac") != stage:
                    continue
                return _failure(
                    job_id=job.job_id,
                    chunk_id=chunk_id,
                    stage=stage,
                    code=injection.get("code", ErrorCode.E_INJECTED_FAULT),
                    message=injection.get(
                        "message", f"deterministic fault injected at {stage}"
                    ),
                    detail={"injected": True, **dict(injection.get("detail", {}))},
                )
        stage_hint = "fixed_mac"
        output, stats = compute_chunk(job, chunk, config, reduction_mode)
        return {
            "ok": True,
            "job_id": job.job_id,
            "chunk_id": chunk_id,
            "output": output,
            "stats": stats,
        }
    except DomainError as exc:
        return _failure(
            job_id=job_id,
            chunk_id=chunk_id,
            stage=exc.stage or stage_hint,
            code=exc.code,
            message=exc.message,
            detail=exc.detail,
        )
    except BaseException as exc:  # Worker boundary must always return structured data.
        frames = traceback.format_exc(limit=4)
        return _failure(
            job_id=job_id,
            chunk_id=chunk_id,
            stage=stage_hint,
            code=ErrorCode.E_WORKER_FAILURE,
            message=f"{type(exc).__name__}: {exc}",
            detail={"traceback_tail": frames[-1200:]},
        )


def _output_checksum(values: np.ndarray) -> str:
    return hashlib.sha256(
        np.ascontiguousarray(values.astype("<f8")).tobytes()
    ).hexdigest()


def _job_summary(
    job: Job,
    plan: dict[str, Any],
    output: np.ndarray | None,
    chunk_errors: list[dict[str, Any]],
    chunk_stats: list[dict[str, Any]],
) -> dict[str, Any]:
    successful_chunks = plan["chunk_count"] - len(
        {error["chunk_id"] for error in chunk_errors}
    )
    failed = bool(chunk_errors) or output is None
    summary: dict[str, Any] = {
        "job_id": job.job_id,
        "status": "failed" if failed else "ok",
        "output_shape": list(job.output_shape),
        "output_dtype": plan["accumulator"]["output_dtype"],
        "chunk_count": plan["chunk_count"],
        "chunks_ok": successful_chunks if not failed else successful_chunks,
        "chunks_failed": len({error["chunk_id"] for error in chunk_errors}),
        "merge_order": list(range(plan["chunk_count"])),
        "worker_errors": chunk_errors,
        "segment_count": sum(
            int(stats.get("segment_count", 0)) for stats in chunk_stats
        ),
    }
    if failed:
        summary["output_checksum"] = None
        summary["max_abs_output"] = None
        summary["mean_abs_output"] = None
        summary["partial_result_discarded"] = True
    else:
        assert output is not None
        summary["output_checksum"] = _output_checksum(output)
        summary["max_abs_output"] = float(np.max(np.abs(output.astype(np.float64))))
        summary["mean_abs_output"] = float(np.mean(np.abs(output.astype(np.float64))))
        summary["partial_result_discarded"] = False
    return summary


def execute_plan(
    jobs_by_id: dict[str, Job],
    plan_payload: dict[str, Any],
    execution_config: ExecutionConfig,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    admitted = plan_payload["admitted"]
    if not admitted:
        summary = {
            "format": "qconv-result-summary-v1",
            "engine": "processpool",
            "workers_used": 0,
            "chunks_dispatched": 0,
            "chunks_ok": 0,
            "chunks_failed": 0,
            "admitted_job_ids": [],
            "completed_job_ids": [],
            "failed_job_ids": [],
            "jobs": [],
            "worker_errors": [],
            "merge_policy": "sort by (plan order, chunk_id)",
            "result_digest": checksum(
                {"admitted_job_ids": [], "jobs": [], "worker_errors": []}
            ),
        }
        return {}, summary

    scheduler_payload = plan_payload["policy"]
    tasks: list[dict[str, Any]] = []
    plan_by_job = {item["job_id"]: item for item in admitted}
    plan_order = {item["job_id"]: index for index, item in enumerate(admitted)}
    for job_plan in admitted:
        job_id = job_plan["job_id"]
        job = jobs_by_id[job_id]
        for chunk in sorted(job_plan["chunks"], key=lambda item: item["chunk_id"]):
            tasks.append(
                {
                    "job_id": job_id,
                    "job": job.to_dict(),
                    "chunk": chunk,
                    "scheduler_config": scheduler_payload,
                    "reduction_mode": job_plan["accumulator"]["merge_mode"],
                    "fault_injections": [
                        dict(item) for item in execution_config.fault_injections
                    ],
                }
            )
    tasks.sort(
        key=lambda task: (
            plan_order[task["job_id"]],
            int(task["chunk"]["chunk_id"]),
        )
    )
    max_processes = max(
        1,
        min(
            execution_config.max_processes,
            plan_payload["worker_slots_used"],
            len(tasks),
        ),
    )
    raw_results: dict[tuple[str, int], dict[str, Any]] = {}
    pool_broken = False
    try:
        with ProcessPoolExecutor(max_workers=max_processes) as pool:
            futures = {pool.submit(execute_chunk_task, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                job_id = task["job_id"]
                chunk_id = int(task["chunk"]["chunk_id"])
                try:
                    raw_results[(job_id, chunk_id)] = future.result()
                except BaseException as exc:
                    raw_results[(job_id, chunk_id)] = _failure(
                        job_id=job_id,
                        chunk_id=chunk_id,
                        stage="dispatch",
                        code=ErrorCode.E_WORKER_FAILURE,
                        message=f"process pool future failed: {type(exc).__name__}: {exc}",
                    )
    except BrokenProcessPool as exc:
        pool_broken = True
        for task in tasks:
            raw_results[(task["job_id"], int(task["chunk"]["chunk_id"]))] = _failure(
                job_id=task["job_id"],
                chunk_id=int(task["chunk"]["chunk_id"]),
                stage="dispatch",
                code=ErrorCode.E_WORKER_FAILURE,
                message=f"process pool terminated: {exc}",
            )

    outputs: dict[str, np.ndarray] = {}
    job_summaries: list[dict[str, Any]] = []
    all_worker_errors: list[dict[str, Any]] = []
    for job_plan in admitted:
        job_id = job_plan["job_id"]
        job = jobs_by_id[job_id]
        chunk_errors: list[dict[str, Any]] = []
        chunk_stats: list[dict[str, Any]] = []
        successful: list[tuple[dict[str, Any], np.ndarray]] = []
        for chunk in sorted(job_plan["chunks"], key=lambda item: item["chunk_id"]):
            chunk_id = int(chunk["chunk_id"])
            result = raw_results.get((job_id, chunk_id))
            if result is None:
                result = _failure(
                    job_id=job_id,
                    chunk_id=chunk_id,
                    stage="dispatch",
                    code=ErrorCode.E_WORKER_FAILURE,
                    message="worker result is missing",
                )
            if result.get("ok"):
                successful.append((chunk, result["output"]))
                chunk_stats.append(dict(result.get("stats", {})))
            else:
                error = {
                    "job_id": job_id,
                    "chunk_id": chunk_id,
                    "stage": result.get("stage"),
                    "code": result.get("code"),
                    "message": result.get("message"),
                    "detail": result.get("detail", {}),
                }
                chunk_errors.append(error)
                all_worker_errors.append(error)
        output: np.ndarray | None = None
        if not chunk_errors:
            dtype = (
                np.float32
                if job_plan["accumulator"]["output_dtype"] == "float32"
                else np.float64
            )
            output = merge_chunks(job, successful, dtype)
            outputs[job_id] = output
        job_summaries.append(
            _job_summary(job, job_plan, output, chunk_errors, chunk_stats)
        )

    completed_job_ids = [
        item["job_id"] for item in job_summaries if item["status"] == "ok"
    ]
    failed_job_ids = [
        item["job_id"] for item in job_summaries if item["status"] == "failed"
    ]
    body = {
        "admitted_job_ids": [item["job_id"] for item in admitted],
        "jobs": job_summaries,
        "worker_errors": all_worker_errors,
    }
    summary = {
        "format": "qconv-result-summary-v1",
        "engine": "processpool",
        "workers_used": max_processes,
        "pool_broken": pool_broken,
        "chunks_dispatched": len(tasks),
        "chunks_ok": sum(item["chunks_ok"] for item in job_summaries),
        "chunks_failed": sum(item["chunks_failed"] for item in job_summaries),
        "admitted_job_ids": body["admitted_job_ids"],
        "completed_job_ids": completed_job_ids,
        "failed_job_ids": failed_job_ids,
        "jobs": job_summaries,
        "worker_errors": all_worker_errors,
        "merge_policy": "plan order, then ascending chunk_id",
        "result_digest": checksum(body),
    }
    return outputs, summary

