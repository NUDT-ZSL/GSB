from __future__ import annotations

import copy
from collections import Counter
from typing import Any

from . import SNAPSHOT_VERSION
from .pipeline import run_edit
from .revision import plan_revision
from .scheduler import adjudicate_batch, estimate_task, plan_batches, run_process_pool


def compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    compact = dict(summary)
    if "blocks" in compact and isinstance(compact["blocks"], list):
        compact["blocks"] = [
            {k: v for k, v in block.items() if k != "pixel_indices"}
            for block in compact["blocks"]
        ]
    return compact


def baseline_report(tasks: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    task_reports = []
    code_counts: Counter[str] = Counter()
    spectrum_totals: Counter[str] = Counter()
    precision_rmse: dict[str, dict[str, list[float]]] = {}
    dominant_counts: dict[str, Counter[str]] = {}
    byte_identical_count = 0

    for task in tasks:
        result = run_edit(task, params, profiles=("float64", "float32", "fixed"))
        if "summary" not in result:
            code = result.get("code", "E_SCHEMA_INVALID")
            code_counts[code] += 1
            task_reports.append(result)
            continue
        summary = result["summary"]
        task_reports.append(compact_summary(summary))
        code_counts[summary["code"]] += 1
        if summary["block_match"]["byte_identical"]:
            byte_identical_count += 1
        for band in summary["block_match"].get("block_deviation_spectrum", []):
            spectrum_totals[band["band"]] += int(band["count"])
        for profile, diagnostic in summary.get("precision", {}).items():
            precision_rmse.setdefault(profile, {})
            dominant_counts.setdefault(profile, Counter())[diagnostic.get("dominant_error_level")] += 1
            for level, stats in diagnostic.get("levels", {}).items():
                precision_rmse[profile].setdefault(level, []).append(float(stats["rmse"]))

    precision_aggregate = {}
    for profile, levels in precision_rmse.items():
        precision_aggregate[profile] = {
            "mean_rmse_by_level": {
                level: sum(values) / len(values) for level, values in sorted(levels.items())
            },
            "dominant_error_level_counts": dict(dominant_counts.get(profile, {})),
        }

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "task_count": len(tasks),
        "committed_or_diagnosed": byte_identical_count,
        "byte_identical_block_full_count": byte_identical_count,
        "all_blocks_match_float64": byte_identical_count == len(tasks),
        "business_code_counts": dict(code_counts),
        "block_deviation_spectrum": [
            {"band": band, "count": spectrum_totals.get(band, 0)}
            for band in [
                "exact_zero", "(0,1e-12]", "(1e-12,1e-8]", "(1e-8,1e-5]",
                "(1e-5,1e-3]", "(1e-3,1e-1]", "(1e-1,1]", "(1,inf)",
            ]
        ],
        "precision_aggregate": precision_aggregate,
        "tasks": task_reports,
    }


def parallel_report(
    tasks: list[dict[str, Any]],
    params: dict[str, Any],
    *,
    workers: int | None = None,
    batch_size: int | None = None,
) -> dict[str, Any]:
    plans = plan_batches(tasks, params, batch_size=batch_size, worker_override=workers)
    tasks_by_id = {str(task["edit_id"]): task for task in tasks}
    batch_results = []
    all_results = []
    for plan in plans:
        results = run_process_pool(plan, tasks_by_id, params, profiles=("float64", "float32", "fixed"))
        batch_results.append({
            "batch_index": plan["batch_index"],
            "dispatch_order": plan["dispatch_order"],
            "merge_order": plan["merge_order"],
            "granted_workers": plan["granted_workers"],
            "peak_concurrent_resident_bytes": plan["peak_concurrent_resident_bytes"],
            "result_count": len(results),
            "all_ok": all(item.get("ok") for item in results),
            "output_hashes": {item["edit_id"]: item.get("hashes", {}).get("float64_output") for item in results},
        })
        all_results.extend(results)
    canonical_plans_a = [
        {k: plan[k] for k in ("batch_index", "dispatch_order", "merge_order", "granted_workers")}
        for plan in plans
    ]
    canonical_plans_b = [
        {k: plan[k] for k in ("batch_index", "dispatch_order", "merge_order", "granted_workers")}
        for plan in plan_batches(tasks, params, batch_size=batch_size, worker_override=workers)
    ]
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "process_pool_used": True,
        "batch_count": len(plans),
        "result_count": len(all_results),
        "all_ok": all(result.get("ok") for result in all_results),
        "scheduling_reproducible": canonical_plans_a == canonical_plans_b,
        "batches": batch_results,
        "budget_plans": plans,
    }


def budget_probe_report(tasks: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    tight_params = copy.deepcopy(params)
    tight_params["budget"].update({
        "worker_count": 4,
        "max_total_resident_bytes": 680 * 1024,
        "single_block_max_bytes": 96 * 1024,
    })
    tight_plan = adjudicate_batch(0, tasks[:10], tight_params, worker_override=4)

    estimated = [(estimate_task(task, params), task) for task in tasks]
    estimated = [(estimate, task) for estimate, task in estimated if estimate.get("estimable")]
    heavy_task = max(estimated, key=lambda item: item[0]["resident_bytes"])[1]
    serial_params = copy.deepcopy(params)
    serial_params["budget"]["max_total_resident_bytes"] = 520 * 1024
    serial_plan = adjudicate_batch(3, [heavy_task], serial_params)

    progress_params = copy.deepcopy(tight_params)
    progress_params["policy"] = copy.deepcopy(params["policy"])
    progress_params["policy"].update({
        "priority": ["progress", "memory", "quality"],
        "allow_fast_validation": True,
    })
    progress_plan = adjudicate_batch(4, tasks[:10], progress_params, worker_override=4)

    huge_task = {
        "edit_id": "probe-memory-budget",
        "image_seed": 1,
        "image_shape": [4096, 4096, 3],
        "mask": {"polygons": [[[10, 10], [4000, 12], [2000, 4000]]]},
        "prompt": "texture",
        "style_strength": 0.5,
        "iterations": 1,
        "block_size": 64,
        "dtype_profile": "float64",
        "revision": 1,
        "requested_block_concurrency": 8,
    }
    memory_params = copy.deepcopy(params)
    memory_params["policy"]["allow_serial"] = True
    memory_plan = adjudicate_batch(1, [huge_task], memory_params)

    concurrency_task = copy.deepcopy(tasks[0])
    concurrency_task["edit_id"] = "probe-block-concurrency"
    concurrency_task["requested_block_concurrency"] = 99
    strict_params = copy.deepcopy(params)
    strict_params["budget"]["strict_block_concurrency"] = True
    concurrency_plan = adjudicate_batch(2, [concurrency_task], strict_params)

    return {
        "tight_conflict": {
            "record": tight_plan,
            "affected_task_ids": tight_plan["affected_task_ids"],
            "rejected_task_ids": tight_plan["rejected_task_ids"],
        },
        "serial_degradation": {
            "record": serial_plan,
            "observed_categories": serial_plan["decisions"][0].get("degradation_categories", []),
        },
        "policy_tradeoff": {
            "quality_first_validation": tight_plan["validation_decision"],
            "progress_first_validation": progress_plan["validation_decision"],
            "progress_record": progress_plan,
        },
        "impossible_memory": {
            "record": memory_plan,
            "expected_code": "E_MEMORY_BUDGET",
            "observed_code": memory_plan["decisions"][0]["code"],
        },
        "strict_concurrency": {
            "record": concurrency_plan,
            "expected_code": "E_BLOCK_CONCURRENCY",
            "observed_code": concurrency_plan["decisions"][0]["code"],
        },
    }


def invalid_code_report(invalid_cases: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    records = []
    for task in invalid_cases:
        edit_id = task["edit_id"]
        if edit_id in {"invalid-memory-budget", "invalid-block-concurrency"}:
            strict_params = copy.deepcopy(params)
            if edit_id == "invalid-block-concurrency":
                strict_params["budget"]["strict_block_concurrency"] = True
            plan = adjudicate_batch(0, [task], strict_params)
            decision = plan["decisions"][0]
            records.append({"edit_id": edit_id, "code": decision["code"], "status": decision["status"]})
        else:
            result = run_edit(task, params, profiles=())
            records.append({
                "edit_id": edit_id,
                "code": result.get("code"),
                "message": result.get("message"),
            })
    counts = Counter(record["code"] for record in records)
    return {"records": records, "code_counts": dict(counts)}


def revision_report(
    old_tasks: list[dict[str, Any]],
    revised_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    return plan_revision(old_tasks, revised_tasks)
