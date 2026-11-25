from __future__ import annotations

import copy
import json
import shutil
import threading
from pathlib import Path
from typing import Any

import numpy as np

from .clock import LogicalClock
from .errors import AppError, ErrorCode, ErrorLedger
from .io_utils import load_reference, save_image_bundle, write_json
from .metadata import verify_v2_checksums
from .pipeline import build_context, run_global
from .planner import plan_blocks
from .samples import generate_all, sample_config, sample_tasks
from .selfcheck import run_cross_module_self_check
from .service import DiagnosticService
from .snapshots import SnapshotStore


REQUIRED_ERROR_CODES = (
    ErrorCode.LATENCY_BUDGET,
    ErrorCode.BLOCK_MISMATCH,
    ErrorCode.ANCHOR_STRENGTH,
    ErrorCode.REFERENCE_SHAPE,
    ErrorCode.SNAPSHOT_ROLLBACK,
    ErrorCode.STAGE_BUSY,
    ErrorCode.RESOURCE_LIMIT,
)


def _compact_plan(plan: dict[str, Any]) -> dict[str, Any]:
    estimate = plan["selected_estimate"]
    return {
        "grid": plan["grid"],
        "center_block_index": plan["center_block_index"],
        "generation_order": plan["generation_order"],
        "selected_estimate": {
            "block_pixels": estimate["block_pixels"],
            "estimated_first_block_ms": estimate["estimated_first_block_ms"],
            "observed_or_none": None,
            "halo_cells_per_side": estimate["halo_cells_per_side"],
            "sampler_steps": estimate["sampler_steps"],
            "coupling_scale": estimate["coupling_scale"],
            "estimated_block_memory_bytes": estimate["estimated_block_memory_bytes"],
            "latency_feasible": estimate["latency_feasible"],
            "memory_feasible": estimate["memory_feasible"],
        },
        "adjudication": plan["adjudication"],
        "candidates": [
            {
                "grid": item["grid_text"],
                "block_pixels": item["block_pixels"],
                "estimated_ms": round(item["estimated_first_block_ms"], 6),
                "memory_bytes": item["estimated_block_memory_bytes"],
                "latency_feasible": item["latency_feasible"],
                "memory_feasible": item["memory_feasible"],
                "boundary_risk_score": round(item["boundary_risk_score"], 6),
            }
            for item in plan["candidate_evaluations"]
        ],
    }


def _determinism_check(task: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    context = build_context(task, config, reference=None, anchor_strength=0.0)
    first = run_global(task, config, context=context, history=True)
    second = run_global(task, config, context=context, history=True)
    intermediates = [
        a.tobytes() == b.tobytes()
        for a, b in zip(first.get("history", []), second.get("history", []))
    ]
    return {
        "ok": first["image_digest"] == second["image_digest"] and all(intermediates),
        "image_digest": first["image_digest"],
        "intermediate_count": len(intermediates),
        "all_intermediate_fields_byte_identical": all(intermediates),
    }


def _task_acceptance(index: int, task: dict[str, Any], samples_dir: Path, output_dir: Path,
                     error_ledger: ErrorLedger) -> tuple[dict[str, Any], DiagnosticService]:
    config = sample_config()
    service_dir = output_dir / "runtime" / task["task_id"]
    service = DiagnosticService(
        workspace=service_dir,
        config=config,
        tasks=[task],
        error_ledger=error_ledger,
    )
    reference_path = samples_dir / "references" / f"{task['task_id']}.npy"
    service.set_reference(task["task_id"], reference_path)

    submission = service.submit_plan(task["task_id"])
    center = service.preview_center(task["task_id"], anchor_strength=float(config["anchor"]["default_strength"]))
    remaining = service.preview_remaining(
        task["task_id"], anchor_strength=float(config["anchor"]["default_strength"])
    )
    if not remaining["ok"]:
        raise AppError(
            "E_PREVIEW_FAILED",
            "concurrent remaining-block preview failed",
            {"task_id": task["task_id"], "failures": remaining["failures"]},
        )
    verification = service.verify_blocks(
        task["task_id"], anchor_strength=float(config["anchor"]["default_strength"])
    )
    self_check = run_cross_module_self_check(service, task["task_id"])
    determinism = _determinism_check(task, config)

    stitched, _, _ = service.stitch_cached(task["task_id"])
    global_result = service.generate_global(
        task["task_id"], anchor_strength=float(config["anchor"]["default_strength"])
    )
    image_dir = output_dir / "images"
    stitched_u8 = _stitch_u8(stitched, global_result["image_u8"])
    stitched_files = save_image_bundle(
        image_dir / f"{task['task_id']}_stitched", stitched, stitched_u8
    )
    global_files = save_image_bundle(image_dir / f"{task['task_id']}_global", global_result["image"], global_result["image_u8"])

    plan_summary = _compact_plan(submission["plan"])
    plan_summary["selected_estimate"]["observed_or_none"] = center["timing"]["observed_generation_ms"]
    report = {
        "task_id": task["task_id"],
        "ok": bool(
            remaining["ok"]
            and verification["ok"]
            and self_check["ok"]
            and determinism["ok"]
        ),
        "plan": plan_summary,
        "center_preview": {
            "block_index": center["block_index"],
            "pixel_interval": center["bounds"]["half_open_text"],
            "timing": center["timing"],
            "cache_key": center["cache_key"],
            "product_hash": center["product_hash"],
            "ready_snapshot_version": center["ready_snapshot"]["version"],
        },
        "remaining_preview": {
            "success_count": remaining["success_count"],
            "failure_count": remaining["failure_count"],
            "ready_blocks": remaining["ready_blocks"],
        },
        "consistency": {
            "ok": verification["ok"],
            "bytewise_equal": verification["bytewise_equal"],
            "stitched_digest": verification["stitched_digest"],
            "global_digest": verification["global_digest"],
        },
        "self_check": self_check,
        "determinism": determinism,
        "snapshot_versions": {
            "fusion": submission["fusion_snapshot"]["version"],
            "plan": submission["plan_snapshot"]["version"],
            "ready_blocks_latest": service.snapshots.current_version("ready_blocks"),
        },
        "artifacts": {"stitched": stitched_files, "global": global_files},
    }
    return report, service


def _stitch_u8(stitched_float: np.ndarray, reference_u8: np.ndarray) -> np.ndarray:
    # Re-quantize the stitched float image through the same rounding rule.
    return np.rint(np.clip(stitched_float, 0.0, 1.0) * 255.0).astype(np.uint8)


def _latency_scenario(tasks: list[dict[str, Any]], error_ledger: ErrorLedger,
                      output_dir: Path) -> dict[str, Any]:
    config = sample_config()
    service = DiagnosticService(
        workspace=output_dir / "runtime" / "latency",
        config=config,
        tasks=tasks,
        error_ledger=error_ledger,
    )
    task_id = "tdc-001"
    service.submit_plan(task_id)
    center = service.preview_center(task_id)
    other_index = next(
        block["block_index"] for block in service.active_plan["blocks"]
        if block["block_index"] != center["block_index"]
    )
    service.config["latency_budget_ms"] = 0.0
    try:
        service.preview_block(task_id, other_index, enforce_budget=True)
        return {"ok": False, "scenario": "zero_budget", "error_code": None}
    except AppError as error:
        strict_config = copy.deepcopy(config)
        strict_config["latency_budget_ms"] = 0.35
        strict_plan = plan_blocks(next(t for t in tasks if t["task_id"] == "tdc-002"), strict_config)
        return {
            "ok": error.code == ErrorCode.LATENCY_BUDGET,
            "scenario": "observed_generation_exceeds_zero_budget",
            "error": error.to_record(),
            "first_successful_block": center["block_index"],
            "requested_late_block": other_index,
            "strict_plan_grid": strict_plan["grid"],
            "strict_plan_adjudication": strict_plan["adjudication"],
        }


def _anchor_scenario(tasks: list[dict[str, Any]], samples_dir: Path,
                     error_ledger: ErrorLedger) -> dict[str, Any]:
    task = next(t for t in tasks if t["task_id"] == "tdc-001")
    config = sample_config()
    reference = load_reference(samples_dir / "references" / "tdc-001.npy")
    invalid_strength = None
    wrong_shape = None
    try:
        build_context(task, config, reference=reference, anchor_strength=1.75)
    except AppError as error:
        invalid_strength = error.to_record()
        error_ledger.record(error)

    wrong_reference = np.zeros((7, 11, 3), dtype=np.float32)
    try:
        build_context(task, config, reference=wrong_reference, anchor_strength=0.2)
    except AppError as error:
        wrong_shape = error.to_record()
        error_ledger.record(error)

    zero_context = build_context(task, config, reference=reference, anchor_strength=0.0)
    none_context = build_context(task, config, reference=None, anchor_strength=0.0)
    zero_result = run_global(task, config, context=zero_context)
    none_result = run_global(task, config, context=none_context)
    anchored_context = build_context(task, config, reference=reference, anchor_strength=0.22)
    anchored_result = run_global(task, config, context=anchored_context)
    return {
        "ok": (
            invalid_strength is not None
            and invalid_strength["error_code"] == ErrorCode.ANCHOR_STRENGTH
            and wrong_shape is not None
            and wrong_shape["error_code"] == ErrorCode.REFERENCE_SHAPE
            and zero_result["image_digest"] == none_result["image_digest"]
            and anchored_result["image_digest"] != none_result["image_digest"]
        ),
        "invalid_strength": invalid_strength,
        "wrong_shape": wrong_shape,
        "zero_strength_equals_no_anchor": zero_result["image_digest"] == none_result["image_digest"],
        "anchored_differs_from_unanchored": anchored_result["image_digest"] != none_result["image_digest"],
        "anchored_digest": anchored_result["image_digest"],
        "unanchored_digest": none_result["image_digest"],
    }


def _snapshot_scenario(error_ledger: ErrorLedger, output_dir: Path) -> dict[str, Any]:
    snapshot_dir = output_dir / "snapshot-version-demo"
    clock = LogicalClock()
    store1 = SnapshotStore(snapshot_dir, clock, error_ledger)
    first = store1.commit("plan", {"demo": "whole-payload-A"})
    second = store1.commit("plan", {"demo": "whole-payload-B"})
    rollback = None
    try:
        store1.commit_explicit_version("plan", first["version"], {"demo": "stale-overwrite"})
    except AppError as error:
        rollback = error.to_record()

    previous_before_restart = copy.deepcopy(store1._memory)  # noqa: SLF001
    store2 = SnapshotStore(snapshot_dir, clock, error_ledger)
    third = store2.commit_explicit_version("plan", second["version"] + 1, {"demo": "whole-payload-C"})

    restart_store = SnapshotStore(snapshot_dir, clock, error_ledger)
    reconciliation = restart_store.reconcile_after_restart(previous_before_restart)

    objects = sorted((snapshot_dir / "objects").glob("plan-v000001-*.json"))
    latest_plan = snapshot_dir / "plan.latest.json"
    old_bytes = objects[0].read_bytes()
    latest_plan.write_bytes(old_bytes)
    rollback_restart = None
    rejected_store = SnapshotStore(snapshot_dir, clock, error_ledger)
    try:
        rejected_store.reconcile_after_restart(restart_store._memory)  # noqa: SLF001
    except AppError as error:
        rollback_restart = error.to_record()

    return {
        "ok": (
            rollback is not None
            and rollback["error_code"] == ErrorCode.SNAPSHOT_ROLLBACK
            and rollback_restart is not None
            and rollback_restart["error_code"] == ErrorCode.SNAPSHOT_ROLLBACK
            and reconciliation["changes"]["plan"]["version_changed"]
        ),
        "versions": {"v1": first["version"], "v2": second["version"], "v3": third["version"]},
        "explicit_rollback": rollback,
        "restart_reconciliation": reconciliation["changes"],
        "restart_rollback": rollback_restart,
        "ledger_path": str(snapshot_dir / "snapshot_ledger.jsonl"),
    }


def _stage_and_resource_scenarios(tasks: list[dict[str, Any]], error_ledger: ErrorLedger,
                                  output_dir: Path) -> dict[str, Any]:
    config = sample_config()
    stage_service = DiagnosticService(
        output_dir / "runtime" / "stage", config=config, tasks=tasks, error_ledger=error_ledger
    )
    stage_service.submit_plan("tdc-003")
    busy_result: dict[str, Any] = {}

    def conflicting_submit() -> None:
        try:
            stage_service.submit_plan("tdc-002")
        except AppError as error:
            busy_result.update(error.to_record())

    with stage_service.submission_lease():
        thread = threading.Thread(target=conflicting_submit, name="conflicting-submit")
        thread.start()
        thread.join(timeout=2.0)

    tiny_config = copy.deepcopy(config)
    tiny_config["resource_limits"]["max_total_pixels"] = 128
    resource_plan_error = None
    try:
        plan_blocks(tasks[0], tiny_config)
    except AppError as error:
        resource_plan_error = error.to_record()
        error_ledger.record(error)

    parallel_config = copy.deepcopy(config)
    parallel_config["resource_limits"]["max_parallel_tasks"] = 1
    parallel_service = DiagnosticService(
        output_dir / "runtime" / "parallel",
        config=parallel_config,
        tasks=tasks,
        error_ledger=error_ledger,
    )
    parallel_service.submit_plan("tdc-003")
    acquired = parallel_service.acquire_generation_slot(blocking=False)
    parallel_error = None
    try:
        parallel_service.preview_center("tdc-003")
    except AppError as error:
        parallel_error = error.to_record()
    finally:
        if acquired:
            parallel_service.release_generation_slot()

    memory_config = copy.deepcopy(config)
    memory_config["resource_limits"]["max_block_memory_bytes"] = 40_000
    memory_plan = plan_blocks(tasks[0], memory_config)

    return {
        "stage_busy": {
            "ok": busy_result.get("error_code") == ErrorCode.STAGE_BUSY,
            "result": busy_result,
        },
        "total_pixels": {
            "ok": resource_plan_error is not None
            and resource_plan_error["error_code"] == ErrorCode.RESOURCE_LIMIT,
            "error": resource_plan_error,
        },
        "parallel_tasks": {
            "ok": parallel_error is not None
            and parallel_error["error_code"] == ErrorCode.RESOURCE_LIMIT,
            "error": parallel_error,
        },
        "memory_downgrade": {
            "ok": memory_plan["adjudication"]["downgrade_applied"]
            and memory_plan["adjudication"]["downgrade_category"] == "block_memory_limit",
            "selected_grid": memory_plan["grid"],
            "adjudication": memory_plan["adjudication"],
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 本地验收报告",
        "",
        f"- 总结论：{'通过' if report['ok'] else '未通过'}",
        f"- 任务数：{len(report['tasks'])}",
        f"- 错误码覆盖：{', '.join(report['error_codes_present'])}",
        f"- 缺失错误码：{', '.join(report['missing_error_codes']) or '无'}",
        "",
        "## 分块一致性与首块耗时",
        "",
        "| task | grid | center | observed ms | estimate ms | bytewise | self-check |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for task in report["tasks"]:
        plan = task["plan"]
        timing = task["center_preview"]["timing"]
        lines.append(
            f"| `{task['task_id']}` | {plan['grid']['rows']}x{plan['grid']['cols']} | "
            f"{task['center_preview']['block_index']} | {timing['observed_generation_ms']:.4f} | "
            f"{plan['selected_estimate']['estimated_first_block_ms']:.4f} | "
            f"{'✅' if task['consistency']['bytewise_equal'] else '❌'} | "
            f"{'✅' if task['self_check']['ok'] else '❌'} |"
        )
    lines.extend(["", "## 场景结论", ""])
    for name, scenario in report["scenarios"].items():
        ok = scenario.get("ok") if isinstance(scenario, dict) else None
        lines.append(f"- `{name}`: {'✅' if ok else '❌'}")
    lines.extend([
        "",
        "## 账本",
        "",
        f"- 业务错误账本：`{report['error_ledger_path']}`",
        f"- 验收 JSON：`{report['report_path']}`",
        "",
    ])
    return "\n".join(lines)


def run_acceptance(output_dir: str | Path = "artifacts/acceptance",
                   samples_dir: str | Path = "samples") -> dict[str, Any]:
    output_dir = Path(output_dir)
    samples_dir = Path(samples_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = generate_all(samples_dir)
    tasks = sample_tasks()
    error_ledger = ErrorLedger(output_dir / "error_ledger.jsonl")
    metadata_check = verify_v2_checksums(samples_dir / "descriptions" / "v2" / "metadata.sqlite")

    task_reports = []
    primary_service: DiagnosticService | None = None
    task_services: dict[str, DiagnosticService] = {}
    for index, task in enumerate(tasks):
        report, service = _task_acceptance(index, task, samples_dir, output_dir, error_ledger)
        task_reports.append(report)
        task_services[task["task_id"]] = service
        if index == 0:
            primary_service = service

    mismatch_diagnostic = primary_service.mismatch_diagnostic(tasks[0]["task_id"])
    mismatch_ok = (
        mismatch_diagnostic.get("error_code") == ErrorCode.BLOCK_MISMATCH
        and mismatch_diagnostic.get("details", {}).get("first_coordinate") is not None
    )
    scenarios = {
        "latency_budget": _latency_scenario(tasks, error_ledger, output_dir),
        "block_mismatch_diagnostic": {"ok": mismatch_ok, "diagnostic": mismatch_diagnostic},
        "anchor": _anchor_scenario(tasks, samples_dir, error_ledger),
        "snapshot_versioning": _snapshot_scenario(error_ledger, output_dir),
        "stage_and_resources": _stage_and_resource_scenarios(tasks, error_ledger, output_dir),
        "metadata_v2": metadata_check,
    }
    resource_scenario = scenarios["stage_and_resources"]
    resource_scenario["ok"] = all(
        resource_scenario[name]["ok"]
        for name in ("stage_busy", "total_pixels", "parallel_tasks", "memory_downgrade")
    )

    codes = error_ledger.codes()
    missing = [code for code in REQUIRED_ERROR_CODES if code not in codes]
    core_ok = all(item["ok"] for item in task_reports) and metadata_check["ok"]
    scenario_ok = all(bool(item.get("ok")) for item in scenarios.values() if isinstance(item, dict))
    report = {
        "ok": bool(core_ok and scenario_ok and not missing),
        "core_ok": core_ok,
        "scenario_ok": scenario_ok,
        "tasks": task_reports,
        "scenarios": scenarios,
        "error_codes_present": sorted(set(codes)),
        "missing_error_codes": missing,
        "error_records": error_ledger.all(),
        "error_ledger_path": str(output_dir / "error_ledger.jsonl"),
        "sample_manifest": {
            "tasks_jsonl": manifest["tasks_jsonl"],
            "v1": manifest["v1"]["path"],
            "v2_database": manifest["v2"]["database_path"],
        },
    }
    report_path = output_dir / "acceptance-report.json"
    report["report_path"] = str(report_path)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    markdown_path = output_dir / "validation-report.md"
    report["markdown_path"] = str(markdown_path)
    markdown_path.write_text(render_markdown(report), encoding="utf-8")

    write_json(output_dir / "block-plans.json", {
        task_id: service.active_plan for task_id, service in task_services.items()
    })
    write_json(output_dir / "ready-blocks.json", {
        task_id: (service.snapshots.load("ready_blocks") or {}).get("payload")
        for task_id, service in task_services.items()
    })
    write_json(output_dir / "first-block-observations.json", {
        task_id: service.first_block_observation
        for task_id, service in task_services.items()
    })
    write_json(output_dir / "consistency-results.json", {
        item["task_id"]: item["consistency"] for item in task_reports
    })
    return report
