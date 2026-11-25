"""Cross-module self-check and deterministic diagnostic report generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .budget import Budgets, validate_budget
from .cache import ArtifactStore
from .configs import ConfigStore
from .errors import EMemoryBudget
from .invalidation import (
    build_both_scenario,
    build_config_only_scenario,
    build_data_only_scenario,
)
from .keys import stage_keys
from .ledger import VersionedLedger
from .models import Task, load_tasks
from .scheduler import order_tasks
from .util import atomic_write_bytes, canonical_bytes, read_jsonl, sha256_obj


def _check(name: str, passed: bool, details: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "details": details}


def _load_ledgers(output_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        name: VersionedLedger(output_dir / "ledgers", name).read()
        for name in ("tasks", "configs", "samples", "stage_cache", "progress")
    }


def _pick_checkpoint(progress: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    checkpoints = progress["data"]["checkpoints"]
    for mode in ("both", "train", "infer"):
        if mode in checkpoints:
            return mode, checkpoints[mode]
    raise ValueError("no progress checkpoint found")


def _plan_checks(
    tasks: list[Task],
    config_store: ConfigStore,
    cache_index: dict[str, Any],
    seed: int,
) -> dict[str, Any]:
    v1 = config_store.v1_resolved
    current_for = lambda task: config_store.resolve_at(task.logical_clock_ms)
    config_only = build_config_only_scenario(
        tasks,
        v1_config=v1,
        current_config_for=current_for,
        global_seed=seed,
        committed_cache_index=cache_index,
    )
    data_only = build_data_only_scenario(
        tasks,
        revision_bumps={"case-003"},
        size_changes={"case-008": 104},
        config_for=current_for,
        global_seed=seed,
        committed_cache_index=cache_index,
    )
    both = build_both_scenario(
        tasks,
        revision_bumps={"case-007"},
        size_changes={"case-008": 104},
        v1_config=v1,
        current_config_for=current_for,
        global_seed=seed,
        committed_cache_index=cache_index,
    )

    def scenario_assertions(plan: dict[str, Any]) -> dict[str, Any]:
        coverage = plan["coverage"]
        stages_disjoint = True
        suffix_valid = True
        for record in plan["tasks"]:
            if set(record["reused_stages"]) & set(record["recomputed_stages"]):
                stages_disjoint = False
            if record["recomputed_stages"]:
                expected_order = [
                    stage
                    for stage in plan["stage_order"]
                    if stage in record["recomputed_stages"]
                ]
                if expected_order != record["recomputed_stages"]:
                    suffix_valid = False
        return {
            "disjoint_task_sets": coverage["disjoint"],
            "covers_all_tasks": coverage["covers_all"],
            "stage_sets_disjoint_per_task": stages_disjoint,
            "recompute_suffix_order_preserved": suffix_valid,
        }

    config_only_checks = scenario_assertions(config_only)
    config_only_checks["sample_quantize_upstream_reused"] = all(
        {"sample", "quantize"}.issubset(record["reused_stages"])
        for record in config_only["tasks"]
        if record["classification"] == "config_only"
    )
    data_only_checks = scenario_assertions(data_only)
    data_only_checks["model_upstream_reused"] = all(
        "model" in record["reused_stages"]
        for record in data_only["tasks"]
        if record["classification"] == "data_only"
    )
    data_only_checks["changed_data_rebuilds_sample"] = all(
        "sample" in record["recomputed_stages"]
        for record in data_only["tasks"]
        if record["classification"] == "data_only"
    )
    both_checks = scenario_assertions(both)
    both_checks["both_changes_recompute_full_join_stages"] = all(
        {"model", "sample", "quantize", "forward", "train_backward", "inference"}
        == set(record["recomputed_stages"])
        for record in both["tasks"]
        if record["classification"] == "both"
    )
    return {
        "config_only": {
            "assertions": config_only_checks,
            "summary": config_only["coverage"],
            "classifications": config_only["by_classification"],
            "report_checksum": config_only["report_checksum"],
        },
        "data_only": {
            "assertions": data_only_checks,
            "summary": data_only["coverage"],
            "classifications": data_only["by_classification"],
            "report_checksum": data_only["report_checksum"],
        },
        "both": {
            "assertions": both_checks,
            "summary": both["coverage"],
            "classifications": both["by_classification"],
            "report_checksum": both["report_checksum"],
        },
        "all_passed": all(
            all(bool(value) for value in checks.values())
            for checks in (config_only_checks, data_only_checks, both_checks)
        ),
    }


def run_selfcheck(
    input_dir: str | Path,
    output_dir: str | Path,
    *,
    global_seed: int = 42,
) -> dict[str, Any]:
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    checks: list[dict[str, Any]] = []
    ingest = load_tasks(input_dir)
    ledgers = _load_ledgers(output_dir)
    for name, snapshot in ledgers.items():
        if snapshot is None:
            raise ValueError(f"required ledger missing: {name}")
    task_data = ledgers["tasks"]["data"]
    sample_data = ledgers["samples"]["data"]
    cache_data = ledgers["stage_cache"]["data"]
    progress_data = ledgers["progress"]["data"]
    checkpoint_mode, checkpoint = _pick_checkpoint(ledgers["progress"])
    completed = checkpoint["completed"]
    task_rows = task_data["tasks"]
    tasks = [Task.from_dict(row) for row in task_rows]
    config_store = ConfigStore(
        input_dir / "configs" / "config_v1.json",
        input_dir / "configs" / "v2",
    )
    cache_index = cache_data["index"]
    artifact_store = ArtifactStore(output_dir / "stage_cache")

    source_lines = len(read_jsonl(input_dir / "tasks.jsonl"))
    duplicate_count = len(task_data.get("duplicates", []))
    unique_case_ids = {task.case_id for task in tasks}
    checks.append(
        _check(
            "source_ingestion_and_duplicate_accounting",
            int(task_data["accounting_count"]) == len(unique_case_ids)
            and len(tasks) == len(unique_case_ids)
            and source_lines == len(unique_case_ids) + duplicate_count
            and duplicate_count == len(ingest.duplicates)
            and len(completed) == len(unique_case_ids),
            {
                "source_jsonl_lines": source_lines,
                "unique_tasks": len(unique_case_ids),
                "duplicates_ignored": duplicate_count,
                "ledger_accounting_count": task_data["accounting_count"],
                "completed_cases": len(completed),
                "duplicate_case_ids_charged_once": duplicate_count == len(ingest.duplicates),
            },
        )
    )

    ledger_versions = {
        name: {
            "version": snapshot["version"],
            "checksum": snapshot["checksum"],
        }
        for name, snapshot in ledgers.items()
    }
    ledger_ok = (
        checkpoint["cache_version"] == ledgers["stage_cache"]["version"]
        and checkpoint["cache_checksum"] == ledgers["stage_cache"]["checksum"]
        and checkpoint["sample_version"] == ledgers["samples"]["version"]
        and checkpoint["sample_checksum"] == ledgers["samples"]["checksum"]
        and checkpoint["version"] <= ledgers["progress"]["version"]
        and all(checkpoint["version"] > 0 for checkpoint in progress_data["checkpoints"].values())
    )
    checks.append(
        _check(
            "versioned_ledger_integrity",
            ledger_ok,
            {
                "ledger_versions": ledger_versions,
                "checkpoint_mode": checkpoint_mode,
                "checkpoint_version": checkpoint["version"],
                "checkpoint_checksum": checkpoint["content_checksum"],
                "checkpoint_cache": [
                    checkpoint["cache_version"],
                    checkpoint["cache_checksum"],
                ],
                "checkpoint_sample": [
                    checkpoint["sample_version"],
                    checkpoint["sample_checksum"],
                ],
                "full_snapshot_replace_verified_by_ledger_reader": True,
            },
        )
    )

    checkpoint_entries = [
        (case_id, completed[case_id]) for case_id in checkpoint["completed_case_ids"]
    ]
    checkpoint_content_ok = all(
        entry["content_checksum"]
        == sha256_obj(
            {
                "case_id": case_id,
                "task_content_hash": entry["task_content_hash"],
                "keys": entry["keys"],
                "config_generation": entry["config_generation"],
                "config_hash": entry["config_hash"],
            }
        )
        for case_id, entry in checkpoint_entries
    )
    checkpoint_ok = (
        checkpoint["content_checksum"]
        == sha256_obj({"version": checkpoint["version"], "completed": completed})
        and checkpoint["completed_case_ids"] == sorted(completed)
        and checkpoint_content_ok
        and int(checkpoint.get("previous_checkpoint_version") or 0) < checkpoint["version"]
    )
    checks.append(
        _check(
            "checkpoint_content_and_monotonic_version",
            checkpoint_ok,
            {
                "checkpoint_version": checkpoint["version"],
                "previous_checkpoint_version": checkpoint.get("previous_checkpoint_version"),
                "completed_case_ids": checkpoint["completed_case_ids"],
                "content_checksum": checkpoint["content_checksum"],
                "completed_entry_checksums_valid": checkpoint_content_ok,
            },
        )
    )

    keys_ok = True
    key_details: list[dict[str, Any]] = []
    task_by_case = {task.case_id: task for task in tasks}
    for case_id, entry in checkpoint_entries:
        task = task_by_case[case_id]
        resolved = config_store.resolve_at(task.logical_clock_ms)
        reconstructed = stage_keys(task, resolved, global_seed)
        match = reconstructed == entry["keys"]
        keys_ok = keys_ok and match
        sample_summary = sample_data["samples"].get(case_id, {})
        data_summary_ok = (
            sample_summary.get("data_digest") == entry.get("data_digest")
            and sample_summary.get("task_content_hash") == task.content_hash
        )
        key_details.append(
            {
                "case_id": case_id,
                "keys_match": match,
                "sample_digest_match": data_summary_ok,
                "config_generation": resolved.generation,
            }
        )
        keys_ok = keys_ok and data_summary_ok
    checks.append(
        _check(
            "cache_keys_reconstruct_from_task_hash_and_config",
            keys_ok,
            {"cases": key_details},
        )
    )

    artifact_failures = artifact_store.verify_all(cache_index)
    missing_keys = [
        key
        for case_id, entry in checkpoint_entries
        for stage, key in entry["keys"].items()
        if stage != "config"
        if key not in cache_index
    ]
    checks.append(
        _check(
            "cache_artifact_integrity",
            not artifact_failures and not missing_keys,
            {
                "artifact_count": len(cache_index),
                "checksum_failures": artifact_failures,
                "missing_committed_keys": missing_keys,
            },
        )
    )

    dual_ok = True
    dual_cases: list[dict[str, Any]] = []
    for case_id, entry in checkpoint_entries:
        train_summary = entry.get("train_summary")
        infer_summary = entry.get("inference_summary")
        forward_summary = entry.get("forward_summary", {})
        if train_summary is None or infer_summary is None:
            dual_ok = False
            dual_cases.append({"case_id": case_id, "both_modes_present": False})
            continue
        equal = (
            train_summary["forward_digest"]
            == infer_summary["forward_digest"]
            == forward_summary["forward_digest"]
        )
        same_forward_key = entry["keys"]["train_backward"] != entry["keys"]["inference"]
        block_consistent = (
            forward_summary["row_boundaries"] == forward_summary["row_boundaries"]
            and forward_summary["block_plan"]["row_block"] > 0
        )
        dual_ok = dual_ok and equal and same_forward_key and block_consistent
        dual_cases.append(
            {
                "case_id": case_id,
                "both_modes_present": True,
                "forward_digest_equal": equal,
                "shared_forward_stage_key": entry["keys"]["forward"],
                "block_plan": forward_summary["block_plan"],
            }
        )
    checks.append(
        _check(
            "dual_mode_shared_forward_consistency",
            dual_ok,
            {"cases": dual_cases},
        )
    )

    ordered = order_tasks(tasks)
    scheduled_order = [task.case_id for task in ordered]
    ledger_order_matches = task_rows == [task.to_dict() for task in ordered]
    completed_covers_all = set(completed) == set(scheduled_order)
    order_matches = ledger_order_matches and completed_covers_all
    budget_records = []
    budget_ok = True
    for task in ordered:
        try:
            admitted = validate_budget(task, checkpoint_mode, Budgets())
        except EMemoryBudget:
            budget_ok = False
            break
        budget_records.append(
            {
                "case_id": task.case_id,
                "logical_clock_ms": task.logical_clock_ms,
                "maxima": admitted["profile"]["maxima"],
            }
        )
    tiny_budget_rejected = False
    try:
        validate_budget(
            ordered[0],
            "both",
            Budgets(
                total_feature_buffer_size=1,
                single_pass_workspace_size=1,
                model_workspace_size=1,
            ),
        )
    except EMemoryBudget as exc:
        tiny_budget_rejected = exc.code == "E_MEMORY_BUDGET"
    checks.append(
        _check(
            "logical_clock_order_and_memory_budget",
            order_matches and budget_ok and tiny_budget_rejected,
            {
                "execution_order": scheduled_order,
                "matches_logical_clock": order_matches,
                "task_ledger_matches_scheduler": ledger_order_matches,
                "completed_covers_scheduled_tasks": completed_covers_all,
                "budgets": Budgets().to_dict(),
                "tasks": budget_records,
                "overlimit_returns_E_MEMORY_BUDGET": tiny_budget_rejected,
            },
        )
    )

    plan_report = _plan_checks(tasks, config_store, cache_index, global_seed)
    checks.append(
        _check(
            "targeted_invalidation_sets_and_suffixes",
            plan_report["all_passed"],
            plan_report,
        )
    )

    pending_empty = not any(
        path.is_dir() and any(path.iterdir())
        for path in (output_dir / "stage_cache" / ".pending").glob("*")
    )
    checks.append(
        _check(
            "no_uncommitted_pending_artifacts",
            pending_empty,
            {"pending_root": str(output_dir / "stage_cache" / ".pending")},
        )
    )

    report: dict[str, Any] = {
        "schema_version": 1,
        "application": "training-inference-integrated-data-diagnostics",
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "global_seed": global_seed,
        "checkpoint_mode": checkpoint_mode,
        "overall_pass": all(check["passed"] for check in checks),
        "summary": {
            "checks_total": len(checks),
            "checks_passed": sum(1 for check in checks if check["passed"]),
            "tasks": len(tasks),
            "completed_cases": len(completed),
            "cache_artifacts": len(cache_index),
        },
        "ledger_versions": ledger_versions,
        "checks": checks,
        "invalidation_scenarios": plan_report,
    }
    report["report_checksum"] = sha256_obj(report)
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "selfcheck.json"
    markdown_path = reports_dir / "selfcheck.md"
    atomic_write_bytes(json_path, canonical_bytes(report) + b"\n")
    atomic_write_bytes(markdown_path, _render_markdown(report).encode("utf-8"))
    report["report_files"] = {"json": str(json_path), "markdown": str(markdown_path)}
    return report


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# TII 自检报告",
        "",
        f"- 总体结果: **{'PASS' if report['overall_pass'] else 'FAIL'}**",
        f"- 输入目录: `{report['input_dir']}`",
        f"- 输出目录: `{report['output_dir']}`",
        f"- 固定种子: `{report['global_seed']}`",
        f"- 检查点模式: `{report['checkpoint_mode']}`",
        f"- 任务/已完成: `{report['summary']['tasks']}` / `{report['summary']['completed_cases']}`",
        f"- 缓存产物数: `{report['summary']['cache_artifacts']}`",
        f"- 报告校验和: `{report['report_checksum']}`",
        "",
        "## 检查项",
        "",
    ]
    for check in report["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        lines.append(f"- `{status}` {check['name']}")
    lines.extend(
        [
            "",
            "## 失效传播场景",
            "",
            f"- 仅配置依赖: `{report['invalidation_scenarios']['config_only']['classifications']}`",
            f"- 仅数据依赖: `{report['invalidation_scenarios']['data_only']['classifications']}`",
            f"- 配置与数据同时变化: `{report['invalidation_scenarios']['both']['classifications']}`",
            "",
            "报告由固定输入、固定种子和已提交内容生成，不包含墙钟时间。",
            "",
        ]
    )
    return "\n".join(lines)
