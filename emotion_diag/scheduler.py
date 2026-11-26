"""Deterministic parallel scheduling and dual resource-budget admission."""

from __future__ import annotations

import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, List, Sequence

from .config import EngineConfig
from .errors import DiagnosticError, ErrorCode
from .features import feature_memory_bytes


def plan_batches(frame_count: int, batch_frames: int) -> List[Dict[str, int]]:
    if frame_count <= 0:
        return []
    return [
        {
            "batch_index": index,
            "start_frame": start,
            "end_frame": min(frame_count, start + int(batch_frames)),
        }
        for index, start in enumerate(range(0, frame_count, int(batch_frames)))
    ]


def check_admission(
    *,
    frame_count: int,
    resident_feature_bytes: int,
    max_transitions: int,
    max_feature_memory_bytes: int,
    workers: int,
    batch_frames: int,
) -> Dict[str, Any]:
    batches = plan_batches(frame_count, batch_frames)
    dispatch_order = [batch["batch_index"] for batch in batches]
    admission = {
        "admitted": True,
        "frame_count": int(frame_count),
        "planned_transition_count": int(frame_count),
        "max_transitions": int(max_transitions),
        "resident_feature_bytes": int(resident_feature_bytes),
        "max_feature_memory_bytes": int(max_feature_memory_bytes),
        "worker_count": int(workers),
        "batch_frames": int(batch_frames),
        "batch_count": len(batches),
        "dispatch_order": dispatch_order,
        "merge_order": list(dispatch_order),
        "priority": ["E_LOOP_BUDGET", "E_RESOURCE_LIMIT"],
        "degradation_category": "none",
    }

    if frame_count > int(max_transitions):
        admission.update(
            {
                "admitted": False,
                "code": ErrorCode.LOOP_BUDGET,
                "degradation_category": "loop_budget_rejected",
            }
        )
        raise DiagnosticError(
            ErrorCode.LOOP_BUDGET,
            "total state-transition budget would be exceeded",
            {
                "degradation_category": "loop_budget_rejected",
                "planned_transition_count": frame_count,
                "max_transitions": int(max_transitions),
                "priority": "checked_before_resource_limit",
            },
        )

    if resident_feature_bytes > int(max_feature_memory_bytes):
        admission.update(
            {
                "admitted": False,
                "code": ErrorCode.RESOURCE_LIMIT,
                "degradation_category": "resident_feature_memory_rejected",
            }
        )
        raise DiagnosticError(
            ErrorCode.RESOURCE_LIMIT,
            "resident frame-feature memory budget would be exceeded",
            {
                "degradation_category": "resident_feature_memory_rejected",
                "resident_feature_bytes": int(resident_feature_bytes),
                "max_feature_memory_bytes": int(max_feature_memory_bytes),
                "priority": "loop_budget_then_resource_limit",
            },
        )

    return admission


def _pool_worker(task: Dict[str, Any]) -> Dict[str, Any]:
    from .emotions import batch_transition_variants

    variants = batch_transition_variants(
        task["features"],
        task["truth"],
        task["config"],
        0,
        len(task["features"]),
    )
    return {
        "batch_index": task["batch_index"],
        "start_frame": task["start_frame"],
        "end_frame": task["end_frame"],
        "variants": variants,
    }


def run_state_machine(
    features: Sequence[Dict[str, Any]],
    truth: Sequence[Dict[str, Any]],
    config: EngineConfig,
    *,
    use_process_pool: bool | None = None,
    resident_feature_bytes: int | None = None,
) -> Dict[str, Any]:
    if len(features) != len(truth):
        raise DiagnosticError(
            ErrorCode.INVALID_CONFIG,
            "feature and truth frame counts differ",
            {"features": len(features), "truth": len(truth)},
        )

    memory_bytes = (
        int(resident_feature_bytes)
        if resident_feature_bytes is not None
        else feature_memory_bytes(features)
    )
    admission = check_admission(
        frame_count=len(features),
        resident_feature_bytes=memory_bytes,
        max_transitions=int(config["max_transitions"]),
        max_feature_memory_bytes=int(config["max_feature_memory_bytes"]),
        workers=int(config["workers"]),
        batch_frames=int(config["batch_frames"]),
    )
    batches = admission["dispatch_order"] and plan_batches(
        len(features), int(config["batch_frames"])
    )

    tasks: List[Dict[str, Any]] = []
    for batch in batches:
        start = int(batch["start_frame"])
        end = int(batch["end_frame"])
        tasks.append(
            {
                "batch_index": int(batch["batch_index"]),
                "start_frame": start,
                "end_frame": end,
                "features": list(features[start:end]),
                "truth": list(truth[start:end]),
                "config": config.to_dict(),
            }
        )

    worker_count = int(config["workers"])
    if use_process_pool is None:
        use_process_pool = worker_count > 1 and len(tasks) > 1

    results: List[Dict[str, Any]] = []
    if use_process_pool:
        context = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else mp.get_context()
        with ProcessPoolExecutor(max_workers=worker_count, mp_context=context) as executor:
            futures = [executor.submit(_pool_worker, task) for task in tasks]
            for future in futures:
                results.append(future.result())
        results.sort(key=lambda item: int(item["batch_index"]))
    else:
        for task in tasks:
            results.append(_pool_worker(task))

    records: List[Dict[str, Any]] = []
    actual_state_code = 0
    for result in sorted(results, key=lambda item: int(item["batch_index"])):
        variant = result["variants"][str(actual_state_code)]
        records.extend(variant["records"])
        actual_state_code = int(variant["final_state_code"])

    if [int(record["frame_index"]) for record in records] != list(range(len(features))):
        raise DiagnosticError(
            ErrorCode.FRAME_COUNT_MISMATCH,
            "merged state records are not a complete ordered frame sequence",
            {"record_count": len(records), "frame_count": len(features)},
        )

    from .emotions import CODE_STATES

    return {
        "records": records,
        "final_state": CODE_STATES[actual_state_code],
        "admission": admission,
        "batches": batches,
        "dispatch_order": [task["batch_index"] for task in tasks],
        "merge_order": [task["batch_index"] for task in tasks],
        "used_process_pool": bool(use_process_pool),
        "worker_count": worker_count if use_process_pool else 1,
    }
