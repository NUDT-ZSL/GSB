"""End-to-end deterministic scenario and two-scenario experiment orchestration."""

from __future__ import annotations

import copy
from typing import Any

from .budget import build_plan
from .canonical import canonical_sha256
from .config import METHOD_ORDER, EngineConfig, normalize_config
from .errors import EngineError
from .evaluation import (
    assert_result_consistency,
    compare_results,
    compute_metrics,
)
from .scheduler import execute_tasks
from .snapshots import BudgetLedger, SnapshotStore, snapshot_reference
from .validation import Dataset


def _config_input_summary(config: EngineConfig, dataset: Dataset) -> dict[str, Any]:
    groups = sorted(set(dataset.groups))
    return {
        "run_seed": config.run_seed,
        "n_samples": dataset.n_rows,
        "n_features": dataset.n_features,
        "n_injected_anomalies": sum(dataset.injected),
        "n_groups": len(groups),
        "groups": groups,
        "sample_id_set_sha256": canonical_sha256(list(dataset.sample_ids)),
        "sample_source_sha256": dataset.source_sha256,
        "config_sha256": canonical_sha256(config.serializable()),
    }


def _task_lookup(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {task["task_id"]: task for task in plan["tasks"]}


def _envelope_lookup(execution: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {envelope["task_id"]: envelope for envelope in execution["envelopes"]}


def _summary_only(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": result.get("candidate_id"),
        "method": result.get("method"),
        "params": result.get("params"),
        "seed": result.get("seed"),
        "score_checksum": result.get("score_checksum"),
        "score_summary": result.get("score_summary"),
        "metrics": result.get("metrics"),
        "ranking_head": result.get("ranking", [])[:10],
    }


def run_scenario(
    raw_config: dict[str, Any],
    dataset: Dataset,
    *,
    scenario_id: str,
    snapshot_store: SnapshotStore | None = None,
) -> dict[str, Any]:
    store = snapshot_store or SnapshotStore()
    config = normalize_config(raw_config, n_rows=dataset.n_rows)
    plan = build_plan(config, n_rows=dataset.n_rows, n_features=dataset.n_features)
    plan_reference = store.commit(
        "plan",
        {
            "scenario_id": scenario_id,
            "config_sha256": canonical_sha256(config.serializable()),
            "sample_id_set_sha256": canonical_sha256(list(dataset.sample_ids)),
            "plan": plan,
        },
    )

    ledger = BudgetLedger(config.max_comparisons)
    reservation_failures: list[dict[str, Any]] = []
    for task in plan["tasks"]:
        try:
            ledger.reserve(
                task["task_id"],
                int(task["comparison_estimate"]),
                metadata={
                    "method": task["method"],
                    "batch_index": task.get("batch_index"),
                    "degraded": task["degraded"],
                },
            )
        except EngineError as exc:
            reservation_failures.append(exc.to_dict())

    dataset_payload = dataset.serializable()
    parallel_execution = execute_tasks(
        tasks=plan["tasks"],
        dataset_payload=dataset_payload,
        mode="parallel",
        workers=config.workers,
        start_method=config.start_method,
        batches=plan["execution_batches"],
    )
    serial_execution = execute_tasks(
        tasks=plan["tasks"],
        dataset_payload=dataset_payload,
        mode="serial",
        workers=1,
        start_method=config.start_method,
        batches=plan["execution_batches"],
        row_chunk_override=dataset.n_rows,
    )

    parallel_by_id = _envelope_lookup(parallel_execution)
    serial_by_id = _envelope_lookup(serial_execution)
    task_map = _task_lookup(plan)
    injected_by_id = dict(zip(dataset.sample_ids, dataset.injected))
    expected_ids = dataset.sample_ids

    task_reports: list[dict[str, Any]] = []
    method_results: list[dict[str, Any]] = []
    consistency_failures: list[dict[str, Any]] = []
    comparison_failures: list[str] = []
    released_task_ids: list[str] = []

    for task in plan["tasks"]:
        task_id = task["task_id"]
        parallel_envelope = parallel_by_id[task_id]
        serial_envelope = serial_by_id[task_id]
        task_report: dict[str, Any] = {
            "task_id": task_id,
            "candidate_id": task["candidate_id"],
            "method": task["method"],
            "candidate_index": task["candidate_index"],
            "batch_index": task.get("batch_index"),
            "degraded": task["degraded"],
            "degradations": task["degradations"],
            "params": task["params"],
            "original_params": task["original_params"],
            "seed": task["seed"],
            "row_chunk_size": task["row_chunk_size"],
            "resource_estimate": {
                "comparisons": task["comparison_estimate"],
                "memory_bytes": task["memory_estimate"],
            },
            "parallel_status": parallel_envelope["status"],
            "serial_baseline_status": serial_envelope["status"],
            "comparison": None,
            "metric_recheck": None,
        }
        if parallel_envelope["status"] != "ok":
            task_report["error"] = parallel_envelope["error"]
            task_report["status"] = "parallel_failed"
            task_reports.append(task_report)
            continue

        parallel_result = parallel_envelope["result"]
        parallel_result["metrics"] = compute_metrics(
            parallel_result["score_details"],
            injected_by_id,
            quota_fraction=config.quota_fraction,
        )
        try:
            consistency = assert_result_consistency(
                parallel_result,
                expected_ids=expected_ids,
                injected_by_id=injected_by_id,
                quota_fraction=config.quota_fraction,
            )
            task_report["metric_recheck"] = consistency
            task_report["status"] = "ok"
        except EngineError as exc:
            task_report["status"] = "inconsistent"
            task_report["error"] = exc.to_dict()
            consistency_failures.append(exc.to_dict())
            task_reports.append(task_report)
            continue

        if serial_envelope["status"] == "ok":
            serial_result = serial_envelope["result"]
            serial_result["metrics"] = compute_metrics(
                serial_result["score_details"],
                injected_by_id,
                quota_fraction=config.quota_fraction,
            )
            try:
                assert_result_consistency(
                    serial_result,
                    expected_ids=expected_ids,
                    injected_by_id=injected_by_id,
                    quota_fraction=config.quota_fraction,
                )
            except EngineError as exc:
                task_report["status"] = "serial_baseline_inconsistent"
                task_report["serial_baseline_error"] = exc.to_dict()
                consistency_failures.append(exc.to_dict())
                task_reports.append(task_report)
                continue
            deviation = compare_results(parallel_result, serial_result)
            deviation["baseline"] = {
                "mode": "serial",
                "row_chunk_size": dataset.n_rows,
                "params": task["params"],
                "seed": task["seed"],
            }
            task_report["comparison"] = deviation
            if not deviation["consistent"]:
                task_report["status"] = "parallel_serial_mismatch"
                comparison_failures.append(task_id)
        else:
            task_report["serial_baseline_error"] = serial_envelope["error"]
            task_report["status"] = "serial_failed"

        task_reports.append(task_report)
        method_results.append(parallel_result)

    failed_task_ids = {
        report["task_id"]
        for report in task_reports
        if report["status"] not in {"ok"}
    }
    for task_id in sorted(failed_task_ids):
        try:
            ledger.release(task_id, reason="task_failed_without_result")
            released_task_ids.append(task_id)
        except EngineError:
            pass

    ledger_snapshot = ledger.snapshot()
    budget_reference = store.commit(
        "budget",
        {
            "scenario_id": scenario_id,
            "comparison_budget": config.max_comparisons,
            "ledger": ledger_snapshot,
            "plan_estimated_comparisons": plan["totals"]["estimated_comparisons"],
            "released_task_ids": released_task_ids,
            "reservation_failures": reservation_failures,
        },
    )

    method_results.sort(
        key=lambda result: (
            METHOD_ORDER.get(result["method"], 99),
            result["params"].get("seed", 0),
            result["candidate_id"],
        )
    )
    task_reports.sort(
        key=lambda report: (
            METHOD_ORDER.get(report["method"], 99),
            report["candidate_index"],
            report["task_id"],
        )
    )
    deviations = [
        {
            "task_id": report["task_id"],
            "candidate_id": report["candidate_id"],
            "method": report["method"],
            "status": (
                "ok"
                if report["comparison"] is not None and report["comparison"]["consistent"]
                else "failed"
                if report["status"] != "ok"
                else "not_compared"
            ),
            "required": True,
            "declared_tolerance": (report["comparison"] or {}).get("declared_tolerance"),
            "score_checksum_equal": (report["comparison"] or {}).get("score_checksum_equal"),
            "ranking_equal": (report["comparison"] or {}).get("ranking_equal"),
            "max_abs_delta": (report["comparison"] or {}).get("max_abs_delta"),
            "p95_abs_delta": (report["comparison"] or {}).get("p95_abs_delta"),
            "first_score_mismatch": (report["comparison"] or {}).get("first_score_mismatch"),
            "first_rank_mismatch": (report["comparison"] or {}).get("first_rank_mismatch"),
            "first_inconsistency": (report["comparison"] or {}).get("first_inconsistency"),
            "nonzero_delta_count": (report["comparison"] or {}).get("nonzero_delta_count"),
            "abs_delta_histogram": (report["comparison"] or {}).get("abs_delta_histogram"),
        }
        for report in task_reports
    ]

    business_codes: set[str] = set(plan.get("business_codes", []))
    for failure in reservation_failures:
        business_codes.add(failure["business_code"])
    for report in task_reports:
        if "error" in report:
            business_codes.add(report["error"].get("business_code", "E_INTERNAL"))
        if "serial_baseline_error" in report:
            business_codes.add(
                report["serial_baseline_error"].get("business_code", "E_INTERNAL")
            )
    for failure in consistency_failures:
        business_codes.add(failure.get("business_code", "E_INCONSISTENT"))
    if comparison_failures:
        business_codes.add("E_EQUIVALENCE_MISMATCH")

    ok = (
        not reservation_failures
        and not consistency_failures
        and not comparison_failures
        and len(method_results) == len(plan["tasks"])
    )
    if not ok:
        scenario_status = "partial_failure"
    elif plan.get("business_codes"):
        scenario_status = "completed_with_budget_decisions"
    else:
        scenario_status = "completed"

    result_summary = {
        "scenario_id": scenario_id,
        "status": scenario_status,
        "config_sha256": canonical_sha256(config.serializable()),
        "sample_id_set_sha256": canonical_sha256(list(dataset.sample_ids)),
        "admitted_task_ids": plan["admitted_task_ids"],
        "successful_task_ids": [result["candidate_id"] for result in method_results],
        "failed_task_ids": sorted(failed_task_ids),
        "business_codes": sorted(business_codes),
        "method_summaries": [_summary_only(result) for result in method_results],
        "budget_totals": plan["totals"],
        "ledger": {
            "limit": ledger_snapshot["limit"],
            "reserved": ledger_snapshot["reserved"],
            "available": ledger_snapshot["available"],
        },
        "equivalence": {
            "required_tasks": len(plan["tasks"]),
            "verified_tasks": sum(
                1
                for report in task_reports
                if report["comparison"] is not None
                and report["comparison"]["consistent"]
            ),
            "mismatched_task_ids": comparison_failures,
        },
    }
    result_reference = store.commit("result", result_summary)

    return {
        "scenario_id": scenario_id,
        "status": scenario_status,
        "ok": ok,
        "input": _config_input_summary(config, dataset),
        "config": config.serializable(),
        "plan": plan,
        "budget_ledger": ledger_snapshot,
        "scheduling_report": {
            "planner_version": plan["planner_version"],
            "priority_rule": plan["priority_rule"],
            "decisions": plan["decisions"],
            "admitted_task_ids": plan["admitted_task_ids"],
            "rejected_task_ids": plan["rejected_task_ids"],
            "degraded_task_ids": plan["degraded_task_ids"],
            "dispatch_order": plan["dispatch_order"],
            "execution_batches": plan["execution_batches"],
            "totals": plan["totals"],
            "parallel_success": parallel_execution["successful_task_ids"],
            "parallel_failures": parallel_execution["failed_task_ids"],
            "serial_baseline_success": serial_execution["successful_task_ids"],
            "serial_baseline_failures": serial_execution["failed_task_ids"],
        },
        "task_reports": task_reports,
        "methods": method_results,
        "detection_summary": [_summary_only(result) for result in method_results],
        "deviation_spectrum": deviations,
        "consistency": {
            "ok": not consistency_failures,
            "failures": consistency_failures,
            "parallel_serial_all_equal": not comparison_failures
            and len(method_results) == len(plan["tasks"]),
            "chunked_whole_all_equal": not comparison_failures
            and len(method_results) == len(plan["tasks"]),
            "declared_tolerance": 1e-12,
        },
        "business_codes": sorted(business_codes),
        "snapshots": {
            "plan": plan_reference,
            "budget": budget_reference,
            "result": result_reference,
        },
    }


def run_experiment(
    *,
    baseline_config: dict[str, Any],
    conflict_config: dict[str, Any],
    dataset: Dataset,
) -> dict[str, Any]:
    store = SnapshotStore()
    baseline = run_scenario(
        baseline_config,
        dataset,
        scenario_id="baseline",
        snapshot_store=store,
    )
    conflict = run_scenario(
        conflict_config,
        dataset,
        scenario_id="budget_conflict",
        snapshot_store=store,
    )
    overall_ok = baseline["ok"] and conflict["ok"]
    return {
        "schema_version": "anomaly-experiment-v1",
        "ok": overall_ok,
        "status": "completed" if overall_ok else "partial_failure",
        "dataset": {
            "n_samples": dataset.n_rows,
            "n_features": dataset.n_features,
            "n_injected_anomalies": sum(dataset.injected),
            "sample_source_sha256": dataset.source_sha256,
            "sample_id_set_sha256": canonical_sha256(list(dataset.sample_ids)),
        },
        "scenarios": {
            "baseline": baseline,
            "budget_conflict": conflict,
        },
        "snapshots": {
            "current_versions": {
                "plan": store.get("plan")["version"],
                "budget": store.get("budget")["version"],
                "result": store.get("result")["version"],
            },
            "references": store.references(),
            "replacement_policy": "whole-snapshot replacement",
            "history_versions": {
                stage: [snapshot["version"] for snapshot in store.history(stage)]
                for stage in ("plan", "budget", "result")
            },
        },
        "business_codes": sorted(
            set(baseline["business_codes"]) | set(conflict["business_codes"])
        ),
    }


def attach_reproducibility_report(
    core: dict[str, Any],
    *,
    repeat_core: dict[str, Any],
    repeats: int = 2,
) -> dict[str, Any]:
    first = canonical_sha256(core)
    second = canonical_sha256(repeat_core)
    if first != second:
        raise EngineError(
            "E_REPRODUCIBILITY_MISMATCH",
            "reproducibility",
            "two runs with the same seed produced different canonical output",
            details={"first_sha256": first, "second_sha256": second},
        )
    output = copy.deepcopy(core)
    output["reproducibility"] = {
        "checked": True,
        "repeats": repeats,
        "identical": True,
        "core_sha256": first,
        "canonicalization": "sorted-keys/compact/utf8",
    }
    return output
