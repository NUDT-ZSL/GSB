from __future__ import annotations

import math
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from .errors import WorkbenchError
from .geometry import extract_polygons
from .pipeline import run_edit
from .tasks import validate_task


def _polygon_area_and_perimeter(polygon: list[tuple[float, float]]) -> tuple[float, float]:
    area2 = 0.0
    perimeter = 0.0
    count = len(polygon)
    for i in range(count):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % count]
        area2 += x1 * y2 - x2 * y1
        perimeter += math.hypot(x2 - x1, y2 - y1)
    return abs(area2) / 2.0, perimeter


def estimate_task(task: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    edit_id = str(task.get("edit_id", "<unknown>"))
    budget = params.get("budget", {})
    shape = task.get("image_shape", [1, 1, 3])
    height, width = int(shape[0]), int(shape[1])
    block_size = max(1, int(task.get("block_size", 16)))
    k = int(params.get("boundary_neighbors", 8))
    buffer_count = int(budget.get("pixel_buffer_count", 5))

    try:
        validate_task(task, params)
        polygons = extract_polygons(task.get("mask"))
        area = 0.0
        perimeter = 0.0
        for polygon in polygons:
            poly_area, poly_perimeter = _polygon_area_and_perimeter(polygon)
            area += poly_area
            perimeter += poly_perimeter
    except WorkbenchError as exc:
        return {
            "edit_id": edit_id,
            "estimable": False,
            "code": exc.code,
            "details": exc.details,
        }

    canvas_pixels = height * width
    mask_pixels = int(min(canvas_pixels, max(1, math.ceil(area + max(1.0, perimeter)))))
    boundary_pixels = int(max(1, math.ceil(perimeter)))
    context_samples = int(max(1, math.ceil(perimeter * 2.2)))
    block_count = max(1, math.ceil(mask_pixels / float(block_size * block_size)))
    requested_concurrency = int(task.get(
        "requested_block_concurrency",
        min(int(budget.get("max_block_concurrency", 4)), block_count),
    ))

    pixel_bytes = canvas_pixels * 3 * 8 * buffer_count
    context_bytes = context_samples * int(budget.get("context_bytes_per_sample", 96))
    block_area = block_size * block_size
    per_block_bytes = int(
        block_area * (
            3 * 8 * 5              # completion/style/quant/fusion temporaries
            + k * 8                 # weights
            + k * 3 * 8             # source colour contributions
            + 9 * 8                 # 3x3 colour contributions
        ) + int(budget.get("block_overhead_bytes", 4096))
    )
    actual_concurrency = max(1, min(requested_concurrency, int(budget.get("max_block_concurrency", 4))))
    block_bytes = per_block_bytes * actual_concurrency
    resident_bytes = pixel_bytes + context_bytes + block_bytes

    return {
        "edit_id": edit_id,
        "estimable": True,
        "height": height,
        "width": width,
        "canvas_pixels": canvas_pixels,
        "mask_pixels_estimated": mask_pixels,
        "boundary_pixels_estimated": boundary_pixels,
        "context_samples_estimated": context_samples,
        "requested_block_size": block_size,
        "effective_block_size": block_size,
        "block_count_estimated": block_count,
        "requested_block_concurrency": requested_concurrency,
        "effective_block_concurrency": actual_concurrency,
        "pixel_buffer_bytes": int(pixel_bytes),
        "boundary_context_bytes": int(context_bytes),
        "per_block_intermediate_bytes": int(per_block_bytes),
        "block_intermediate_bytes": int(block_bytes),
        "resident_bytes": int(resident_bytes),
    }


def _reestimate_with_block(estimate: dict[str, Any], block_size: int, concurrency: int, params: dict[str, Any]) -> dict[str, Any]:
    budget = params.get("budget", {})
    k = int(params.get("boundary_neighbors", 8))
    block_area = block_size * block_size
    per_block = int(block_area * (
        3 * 8 * 5 + k * 8 + k * 3 * 8 + 9 * 8
    ) + int(budget.get("block_overhead_bytes", 4096)))
    block_bytes = per_block * max(1, concurrency)
    resident = int(estimate["pixel_buffer_bytes"] + estimate["boundary_context_bytes"] + block_bytes)
    block_count = max(1, math.ceil(int(estimate["mask_pixels_estimated"]) / float(block_area)))
    updated = dict(estimate)
    updated.update({
        "effective_block_size": int(block_size),
        "effective_block_concurrency": int(concurrency),
        "per_block_intermediate_bytes": per_block,
        "block_intermediate_bytes": block_bytes,
        "resident_bytes": resident,
        "block_count_estimated": block_count,
    })
    return updated


def validation_strength_decision(policy: dict[str, Any]) -> dict[str, Any]:
    priority = list(policy.get("priority", ["quality", "memory", "progress"]))
    requested = policy.get("validation_strength", "strict")
    quality_rank = priority.index("quality") if "quality" in priority else 0
    progress_rank = priority.index("progress") if "progress" in priority else 2
    final = requested
    rationale = "declared policy preserves strict boundary and final equality checks"
    if progress_rank < quality_rank and policy.get("allow_fast_validation", False):
        final = "standard"
        rationale = "progress precedes quality and fast validation is allowed; redundant probes are reduced but final E_BLOCK_MISMATCH gate remains strict"
    return {
        "requested": requested,
        "final": final,
        "rationale": rationale,
        "final_equality_gate": "strict_float64",
        "seam_risk": "low" if final == "strict" else "managed: fewer intermediate probes, unchanged final gate",
    }


def adjudicate_batch(
    batch_index: int,
    tasks: list[dict[str, Any]],
    params: dict[str, Any],
    *,
    worker_override: int | None = None,
) -> dict[str, Any]:
    budget = params.get("budget", {})
    policy = params.get("policy", {})
    total_budget = int(budget.get("max_total_resident_bytes", 64 * 1024 * 1024))
    hard_cap = int(budget.get("max_block_concurrency", 4))
    single_block_max = int(budget.get("single_block_max_bytes", 1024 * 1024))
    min_block = int(policy.get("minimum_block_size", 4))
    requested_workers = int(worker_override or budget.get("worker_count", 1))
    allow_smaller = bool(policy.get("allow_smaller_block", True))
    allow_serial = bool(policy.get("allow_serial", True))
    strict_concurrency = bool(budget.get("strict_block_concurrency", False))

    ordered_tasks = sorted(tasks, key=lambda task: str(task.get("edit_id", "")))
    decisions: list[dict[str, Any]] = []
    admitted_estimates: list[dict[str, Any]] = []
    basis: list[str] = []

    for task in ordered_tasks:
        edit_id = str(task.get("edit_id", "<unknown>"))
        estimate = estimate_task(task, params)
        if not estimate.get("estimable"):
            decisions.append({
                "edit_id": edit_id,
                "status": "invalid",
                "code": estimate.get("code", "E_SCHEMA_INVALID"),
                "estimate": estimate,
            })
            continue

        changes: list[str] = []
        reasons: list[str] = []
        block_size = int(estimate["requested_block_size"])
        concurrency = int(estimate["requested_block_concurrency"])
        raw_violations: list[str] = []

        if concurrency > hard_cap:
            raw_violations.append("E_BLOCK_CONCURRENCY")
            if allow_serial and not strict_concurrency:
                concurrency = 1
                changes.append("SERIAL")
                reasons.append(
                    f"requested block concurrency {estimate['requested_block_concurrency']} > cap {hard_cap}; serial degradation preserves one-source coefficients"
                )
            else:
                decisions.append({
                    "edit_id": edit_id,
                    "status": "rejected",
                    "code": "E_BLOCK_CONCURRENCY",
                    "raw_violations": raw_violations,
                    "limit": hard_cap,
                    "estimate": estimate,
                    "basis": [f"requested_block_concurrency>{hard_cap} and serial degradation is disabled"],
                })
                continue

        plan = _reestimate_with_block(estimate, block_size, concurrency, params)
        if int(plan["per_block_intermediate_bytes"]) > single_block_max:
            raw_violations.append("SINGLE_BLOCK_MEMORY")
            if allow_smaller:
                while block_size > min_block:
                    block_size = max(min_block, block_size // 2)
                    plan = _reestimate_with_block(estimate, block_size, concurrency, params)
                    if int(plan["per_block_intermediate_bytes"]) <= single_block_max:
                        break
                if int(plan["per_block_intermediate_bytes"]) <= single_block_max:
                    changes.append("SMALLER_BLOCK")
                    reasons.append(
                        f"requested block exceeds single-block cap {single_block_max}; reduced block_size to {block_size}"
                    )

        if int(plan["resident_bytes"]) > total_budget:
            raw_violations.append("E_MEMORY_BUDGET")
            if allow_serial and concurrency > 1:
                concurrency = 1
                plan = _reestimate_with_block(estimate, block_size, concurrency, params)
                changes.append("SERIAL")
                reasons.append("total resident estimate over budget; serial block execution removes concurrent block buffers")
            while allow_smaller and block_size > min_block and int(plan["resident_bytes"]) > total_budget:
                block_size = max(min_block, block_size // 2)
                plan = _reestimate_with_block(estimate, block_size, concurrency, params)
                if "SMALLER_BLOCK" not in changes:
                    changes.append("SMALLER_BLOCK")
                reasons.append(f"resident estimate over budget; reduced block_size to {block_size}")

        if int(plan["resident_bytes"]) > total_budget:
            decisions.append({
                "edit_id": edit_id,
                "status": "rejected",
                "code": "E_MEMORY_BUDGET",
                "raw_violations": raw_violations + ["E_MEMORY_BUDGET"],
                "limit_bytes": total_budget,
                "estimate": plan,
                "basis": reasons + [f"minimum safe resident {plan['resident_bytes']} > budget {total_budget}"],
            })
            continue

        status = "degraded" if changes else "admitted"
        decision = {
            "edit_id": edit_id,
            "status": status,
            "code": "OK",
                "business_codes": raw_violations,
            "degradation_categories": changes,
            "raw_violations": raw_violations,
            "runtime_block_size": block_size,
            "runtime_block_concurrency": concurrency,
            "estimate": plan,
            "basis": reasons or ["requested resources fit declared budget and concurrency cap"],
        }
        decisions.append(decision)
        admitted_estimates.append(plan)

    admitted = [d for d in decisions if d["status"] in {"admitted", "degraded"}]
    granted_workers = min(requested_workers, max(1, len(admitted)))
    worker_reductions: list[dict[str, Any]] = []

    def peak_resident(workers: int) -> int:
        residents = sorted((int(d["estimate"]["resident_bytes"]) for d in admitted), reverse=True)
        return int(sum(residents[:max(1, workers)]))

    while granted_workers > 1 and peak_resident(granted_workers) > total_budget:
        affected = [
            d["edit_id"] for d in admitted
            if int(d["estimate"]["resident_bytes"]) >= sorted(
                [int(x["estimate"]["resident_bytes"]) for x in admitted], reverse=True
            )[:granted_workers][-1]
        ]
        worker_reductions.append({
            "from_workers": granted_workers,
            "to_workers": granted_workers - 1,
            "peak_resident_bytes": peak_resident(granted_workers),
            "budget_bytes": total_budget,
            "affected_task_ids": affected,
        })
        granted_workers -= 1

    peak = peak_resident(granted_workers) if admitted else 0
    if peak > total_budget and admitted:
        # This should already be impossible because each admitted task fits.
        for decision in admitted:
            decision["status"] = "rejected"
            decision["code"] = "E_MEMORY_BUDGET"
        admitted = []

    if worker_reductions:
        basis.append("reduced process-pool workers so peak concurrent resident memory fits budget")
    basis.append(f"policy priority order is {' > '.join(policy.get('priority', [])) or 'quality > memory > progress'}")
    validation_decision = validation_strength_decision(policy)

    return {
        "batch_index": int(batch_index),
        "requested_batch_size": len(tasks),
        "requested_workers": requested_workers,
        "granted_workers": granted_workers if admitted else 0,
        "budget_bytes": total_budget,
        "single_block_max_bytes": single_block_max,
        "max_block_concurrency": hard_cap,
        "peak_concurrent_resident_bytes": peak,
        "batch_logical_resident_sum_bytes": int(sum(
            int(d["estimate"]["resident_bytes"]) for d in admitted
        )),
        "dispatch_order": [d["edit_id"] for d in admitted],
        "merge_order": sorted(d["edit_id"] for d in admitted),
        "decisions": decisions,
        "worker_reductions": worker_reductions,
        "validation_decision": validation_decision,
        "basis": basis,
        "affected_task_ids": sorted({
            d["edit_id"] for d in decisions if d["status"] == "degraded"
        } | {x for wr in worker_reductions for x in wr["affected_task_ids"]}),
        "rejected_task_ids": [d["edit_id"] for d in decisions if d["status"] == "rejected"],
    }


def plan_batches(tasks: list[dict[str, Any]], params: dict[str, Any], *, batch_size: int | None = None,
                 worker_override: int | None = None) -> list[dict[str, Any]]:
    size = int(batch_size or params.get("budget", {}).get("batch_size", 10))
    ordered = sorted(tasks, key=lambda task: str(task.get("edit_id", "")))
    plans = []
    for batch_index, start in enumerate(range(0, len(ordered), size)):
        plans.append(adjudicate_batch(
            batch_index, ordered[start:start + size], params, worker_override=worker_override
        ))
    return plans


def execute_task_worker(payload: dict[str, Any]) -> dict[str, Any]:
    task = payload["task"]
    params = payload["params"]
    block_size_override = payload.get("block_size_override")
    profiles = tuple(payload.get("profiles", ("float64", "float32", "fixed")))
    try:
        result = run_edit(
            task, params,
            block_size_override=block_size_override,
            profiles=profiles,
        )
        if "summary" in result:
            return result["summary"]
        return result
    except WorkbenchError as exc:
        return {"edit_id": str(task.get("edit_id")), "ok": False, **exc.to_dict()}


def run_process_pool(
    batch_plan: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
    params: dict[str, Any],
    *,
    profiles: tuple[str, ...] = ("float64", "float32", "fixed"),
) -> list[dict[str, Any]]:
    dispatch_order = batch_plan["dispatch_order"]
    decisions = {d["edit_id"]: d for d in batch_plan["decisions"]}
    payloads = []
    for edit_id in dispatch_order:
        decision = decisions[edit_id]
        payloads.append({
            "task": tasks_by_id[edit_id],
            "params": params,
            "block_size_override": decision.get("runtime_block_size"),
            "profiles": profiles,
        })
    if not payloads:
        return []
    context = mp.get_context("fork")
    workers = max(1, int(batch_plan["granted_workers"]))
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as executor:
        # executor.map submits in iterable order and yields in that same order.
        results = list(executor.map(execute_task_worker, payloads))
    results_by_id = {result["edit_id"]: result for result in results}
    return [results_by_id[edit_id] for edit_id in batch_plan["merge_order"] if edit_id in results_by_id]
