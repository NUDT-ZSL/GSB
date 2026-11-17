"""Three-scenario end-to-end demonstration and artifact writer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .catalog import PipelineRequest, load_pipeline_dict
from .codes import ALL_CODES
from .errors import PipeError
from .executor import ExecutionResult, compare_results, execute
from .plan import PlanBundle, build_plans
from .sample import divergent_payload, sample_payload
from .snapshot import create_snapshot, load_snapshot, restore_snapshot, save_snapshot
from .util import write_bytes, write_json
from .verifier import verify_snapshot


def _write_execution_artifacts(
    root: Path,
    execution: ExecutionResult,
    *,
    prefix: str | None = None,
) -> None:
    name = prefix or execution.mode
    write_json(root / f"{name}_result.json", execution.to_artifact())
    write_json(root / f"{name}_trace.json", {"mode": execution.mode, "trace": execution.trace, "stats": execution.stats})
    write_bytes(root / f"{name}.bin", execution.canonical_bytes)


def _scenario_summary(mode: str, execution: ExecutionResult) -> dict[str, Any]:
    return {
        "status": "PASS",
        "business_code": "OK",
        "mode": mode,
        "result_rows": execution.stats["result_rows"],
        "result_columns": execution.stats["result_columns"],
        "result_sha256": execution.canonical_sha256,
        "scan_tasks": execution.stats["scan_tasks"],
        "scanned_rows": execution.stats["scanned_rows"],
        "scan_output_rows": execution.stats["scan_output_rows"],
        "peak_task_memory_bytes": execution.stats["peak_task_memory_bytes"],
        "peak_scheduler_reserved_memory_bytes": execution.stats["peak_scheduler_reserved_memory_bytes"],
        "memory_budget_bytes": execution.stats["memory_budget_bytes"],
        "parallelism_limit": execution.stats["parallelism_limit"],
    }


def generate_demo_files(root: str | Path) -> PipelineRequest:
    demo_root = Path(root)
    demo_root.mkdir(parents=True, exist_ok=True)
    write_json(demo_root / "pipeline_input.json", sample_payload())
    write_json(demo_root / "pipeline_input_v2.json", divergent_payload())
    return load_pipeline_dict(sample_payload(), source_path="$.generated_sample")


def _plans_artifact(bundle: PlanBundle) -> dict[str, Any]:
    return {
        "query_summary_sha256": bundle.query_summary_sha256,
        "optimized": {
            "summary_sha256": bundle.optimized_summary_sha256,
            "summary": bundle.optimized_summary,
            "plan": bundle.optimized_plan,
        },
        "baseline": {
            "summary_sha256": bundle.baseline_summary_sha256,
            "summary": bundle.baseline_summary,
            "plan": bundle.baseline_plan,
        },
        "pushdown": bundle.pushdown,
        "pruning": bundle.pruning,
        "estimation": bundle.estimates,
        "split_plan": bundle.split_plan,
        "baseline_split_plan": bundle.baseline_split_plan,
        "indexes": bundle.indexes,
    }


def run_demo(workspace: str | Path = ".") -> dict[str, Any]:
    workspace_path = Path(workspace).resolve()
    demo_root = workspace_path / "artifacts" / "demo"
    demo_root.mkdir(parents=True, exist_ok=True)
    request = generate_demo_files(demo_root)
    bundle = build_plans(request)
    write_json(demo_root / "logical_plans.json", _plans_artifact(bundle))
    write_json(demo_root / "optimized_plan.json", bundle.optimized_plan)
    write_json(demo_root / "baseline_plan.json", bundle.baseline_plan)

    baseline = execute(bundle, mode="baseline")
    _write_execution_artifacts(demo_root, baseline)
    full_scenario = {
        "status": "PASS",
        "business_code": "OK",
        "description": "full unoptimized baseline scan over every declared column",
        **_scenario_summary("baseline", baseline),
    }

    optimized = execute(bundle, mode="optimized")
    _write_execution_artifacts(demo_root, optimized)
    comparison = compare_results(optimized, baseline)
    optimized_scenario = {
        "status": "PASS" if comparison["byte_equal"] and comparison["statistics_consistent"]["ok"] else "FAIL",
        "business_code": "OK",
        "description": "filter pushdown plus scan/join/projection column pruning",
        **_scenario_summary("optimized", optimized),
        "comparison": comparison,
        "pushed_condition_count": len(bundle.pushdown["pushed_conditions"]),
        "pruned_column_count": len(bundle.pruning["dropped_columns"]),
        "split_sha256": bundle.split_plan["split_sha256"],
    }

    snapshot = create_snapshot(
        bundle,
        {"optimized": optimized, "baseline": baseline},
        comparison,
    )
    snapshot_path = demo_root / "session_snapshot.json"
    save_snapshot(snapshot_path, snapshot)

    mutated_request = load_pipeline_dict(divergent_payload(), source_path="$.generated_v2")
    restored = restore_snapshot(snapshot, mutated_request)
    write_json(demo_root / "restore_report.json", restored.report)
    replay_bundle = build_plans(restored.request)
    replay = execute(replay_bundle, mode="optimized")
    _write_execution_artifacts(demo_root, replay, prefix="restore_replay")
    replay_equal = replay.canonical_bytes == optimized.canonical_bytes
    stats_rebuilt = restored.statistics.get("statistics_sha256") == replay_bundle.statistics.get(
        "statistics_sha256"
    )
    restore_scenario = {
        "status": "PASS" if replay_equal and stats_rebuilt else "FAIL",
        "business_code": "OK",
        "description": "restore v2 session by whole-state replacement, then replay optimized execution",
        "restore_report": restored.report,
        "restored_data_version": restored.request.dataset.data_version,
        "replay_result_sha256": replay.canonical_sha256,
        "original_result_sha256": optimized.canonical_sha256,
        "byte_equal_to_original": replay_equal,
        "statistics_rebuilt": stats_rebuilt,
        "scan_tasks": replay.stats["scan_tasks"],
    }

    verification = verify_snapshot(
        snapshot_path,
        output_path=demo_root / "verification.json",
        artifacts_dir=demo_root,
    )
    scenarios = {
        "full": full_scenario,
        "optimized": optimized_scenario,
        "restore_replay": restore_scenario,
    }
    all_pass = all(item["status"] == "PASS" for item in scenarios.values()) and verification["status"] == "PASS"
    report = {
        "status": "PASS" if all_pass else "FAIL",
        "business_code": "OK" if all_pass else "E_INTERNAL",
        "workspace": str(workspace_path),
        "session_id": request.dataset.session_id,
        "data_version": request.dataset.data_version,
        "snapshot_id": snapshot["snapshot_id"],
        "supported_business_codes": list(ALL_CODES),
        "artifact_contract": {
            "logical_plans": "logical_plans.json",
            "optimized_plan": "optimized_plan.json",
            "baseline_plan": "baseline_plan.json",
            "optimized_result": "optimized_result.json",
            "baseline_result": "baseline_result.json",
            "optimized_trace": "optimized_trace.json",
            "baseline_trace": "baseline_trace.json",
            "canonical_bytes": ["optimized.bin", "baseline.bin"],
            "snapshot": "session_snapshot.json",
            "restore_report": "restore_report.json",
            "verification": "verification.json",
        },
        "scenarios": scenarios,
        "independent_verification": {
            "status": verification["status"],
            "code": verification["code"],
            "checks_passed": verification["summary"]["checks_passed"],
            "checks_failed": verification["summary"]["checks_failed"],
        },
        "metrics": {
            "baseline_result_sha256": baseline.canonical_sha256,
            "optimized_result_sha256": optimized.canonical_sha256,
            "byte_equal": optimized.canonical_bytes == baseline.canonical_bytes,
            "split_plan_sha256": bundle.split_plan["split_sha256"],
            "optimized_plan_sha256": bundle.optimized_summary_sha256,
            "baseline_plan_sha256": bundle.baseline_summary_sha256,
        },
    }
    write_json(demo_root / "demo_report.json", report)
    return report


def run_demo_main(workspace: str | Path = ".") -> int:
    try:
        report = run_demo(workspace)
    except PipeError as exc:
        write_json(Path(workspace) / "artifacts" / "demo" / "demo_error.json", exc.to_dict())
        print(exc.to_dict())
        return 2
    print(
        {
            "status": report["status"],
            "business_code": report["business_code"],
            "report": str(Path(workspace).resolve() / "artifacts" / "demo" / "demo_report.json"),
            "byte_equal": report["metrics"]["byte_equal"],
            "verification": report["independent_verification"],
            "scenarios": {name: item["status"] for name, item in report["scenarios"].items()},
        }
    )
    return 0 if report["status"] == "PASS" else 1
