"""End-to-end demo: normal run, forced termination, recovery, query, and self-check."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path
from typing import Any

from .diagnostics import run_selfcheck
from .engine import Engine
from .query import query_case
from .util import atomic_write_json, read_json, sha256_obj


def _case_product_signature(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_content_hash": entry["task_content_hash"],
        "data_digest": entry["data_digest"],
        "config_generation": entry["config_generation"],
        "config_hash": entry["config_hash"],
        "keys": entry["keys"],
        "forward_digest": entry["forward_summary"]["forward_digest"],
        "train_gradient_digest": entry["train_summary"]["gradient_digest"],
        "inference_digest": entry["inference_summary"]["prediction_digest"],
        "content_checksum": entry["content_checksum"],
    }


def compare_workspaces(reference_dir: str | Path, recovered_dir: str | Path) -> dict[str, Any]:
    reference_progress = read_json(Path(reference_dir) / "ledgers" / "progress.json")
    recovered_progress = read_json(Path(recovered_dir) / "ledgers" / "progress.json")
    reference_mode = "both"
    recovered_mode = "both"
    reference_checkpoint = reference_progress["data"]["checkpoints"][reference_mode]
    recovered_checkpoint = recovered_progress["data"]["checkpoints"][recovered_mode]
    reference_entries = reference_checkpoint["completed"]
    recovered_entries = recovered_checkpoint["completed"]
    compared_cases = sorted(set(reference_entries) | set(recovered_entries))
    mismatches: list[str] = []
    for case_id in compared_cases:
        if case_id not in reference_entries or case_id not in recovered_entries:
            mismatches.append(f"{case_id}: missing on one side")
            continue
        if _case_product_signature(reference_entries[case_id]) != _case_product_signature(
            recovered_entries[case_id]
        ):
            mismatches.append(f"{case_id}: product signature differs")
    reference_cache = read_json(Path(reference_dir) / "ledgers" / "stage_cache.json")
    recovered_cache = read_json(Path(recovered_dir) / "ledgers" / "stage_cache.json")
    reference_index = {
        key: record["checksum"] for key, record in reference_cache["data"]["index"].items()
    }
    recovered_index = {
        key: record["checksum"] for key, record in recovered_cache["data"]["index"].items()
    }
    cache_equal = reference_index == recovered_index
    if not cache_equal:
        mismatches.append("stage cache index differs")
    report = {
        "schema_version": 1,
        "reference_dir": str(reference_dir),
        "recovered_dir": str(recovered_dir),
        "cases_compared": compared_cases,
        "product_signatures_equal": not mismatches,
        "cache_index_equal": cache_equal,
        "final_progress_version_equal": (
            reference_progress["version"] == recovered_progress["version"]
        ),
        "final_cache_version_equal": (
            reference_cache["version"] == recovered_cache["version"]
        ),
        "completed_case_ids_equal": (
            reference_checkpoint["completed_case_ids"]
            == recovered_checkpoint["completed_case_ids"]
        ),
        "mismatches": mismatches,
        "all_equal": (
            not mismatches
            and cache_equal
            and reference_progress["version"] == recovered_progress["version"]
            and reference_cache["version"] == recovered_cache["version"]
            and reference_checkpoint["completed_case_ids"]
            == recovered_checkpoint["completed_case_ids"]
        ),
    }
    report["report_checksum"] = sha256_obj(report)
    return report


def run_demo(
    input_dir: str | Path,
    output_dir: str | Path,
    *,
    recovery_dir: str | Path | None = None,
    global_seed: int = 42,
    clean: bool = True,
) -> dict[str, Any]:
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    recovery_dir = Path(recovery_dir) if recovery_dir else output_dir.parent / "out_recovery"
    if clean:
        for path in (output_dir, recovery_dir):
            shutil.rmtree(path, ignore_errors=True)

    normal = Engine(
        input_dir,
        output_dir,
        mode="both",
        global_seed=global_seed,
        checkpoint_batch_tasks=2,
    ).run()
    forced = Engine(
        input_dir,
        recovery_dir,
        mode="both",
        global_seed=global_seed,
        checkpoint_batch_tasks=2,
        simulate_crash_after_tasks=5,
    ).run()
    progress_after_crash = read_json(recovery_dir / "ledgers" / "progress.json")
    checkpoint_after_crash = progress_after_crash["data"]["checkpoints"]["both"]
    resumed = Engine(
        input_dir,
        recovery_dir,
        mode="both",
        global_seed=global_seed,
        checkpoint_batch_tasks=2,
    ).run()
    comparison = compare_workspaces(output_dir, recovery_dir)

    query_path = output_dir / "reports" / "query-case-007.npz"
    queried = query_case(
        output_dir,
        "case-007",
        mode="both",
        input_dir=input_dir,
        export_npz=query_path,
    )
    normal_selfcheck = run_selfcheck(input_dir, output_dir, global_seed=global_seed)
    output_json_path = output_dir / "reports" / "selfcheck.json"
    first_bytes = output_json_path.read_bytes()
    run_selfcheck(input_dir, output_dir, global_seed=global_seed)
    deterministic = output_json_path.read_bytes() == first_bytes
    recovery_selfcheck = run_selfcheck(input_dir, recovery_dir, global_seed=global_seed)
    recovery_json_path = recovery_dir / "reports" / "selfcheck.json"
    recovery_report = read_json(recovery_json_path)

    report = {
        "schema_version": 1,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "seed": global_seed,
        "normal_run": normal.to_dict(),
        "forced_run": forced.to_dict(),
        "committed_after_forced_stop": checkpoint_after_crash["completed_case_ids"],
        "resumed_run": resumed.to_dict(),
        "recovery_comparison": comparison,
        "query": {
            "case_id": queried["case_id"],
            "available_modes": queried["available_modes"],
            "query_checksum": queried["query_checksum"],
            "export_npz": str(query_path),
        },
        "selfcheck": {
            "normal_overall_pass": normal_selfcheck["overall_pass"],
            "normal_report_checksum": normal_selfcheck["report_checksum"],
            "recovered_overall_pass": recovery_selfcheck["overall_pass"],
            "recovered_report_checksum": recovery_selfcheck["report_checksum"],
            "report_repeatable": deterministic,
        },
        "all_acceptance_checks_pass": (
            normal.status == "completed"
            and forced.status == "forced_termination"
            and resumed.status == "completed"
            and comparison["all_equal"]
            and normal_selfcheck["overall_pass"]
            and recovery_selfcheck["overall_pass"]
            and deterministic
        ),
    }
    report["report_checksum"] = sha256_obj(report)
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(reports_dir / "demo_report.json", report)
    atomic_write_json(reports_dir / "recovery_comparison.json", comparison)
    atomic_write_json(
        reports_dir / "recovery_selfcheck_snapshot.json",
        recovery_report,
    )
    return report
