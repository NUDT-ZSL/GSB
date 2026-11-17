"""Diagnostic report and detailed artifact emission."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, sha256_json
from .engine import WorkbenchEngine, aggregate_invalidation, _serialize_window_keys


def _window_rows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "window_id",
        "session_id",
        "start_turn",
        "status",
        "reason",
        "covered_turn_numbers",
        "missing_turn_numbers",
        "distribution",
        "drift_score",
        "alert",
        "boundary_equal_threshold",
        "alert_rank",
        "baseline_status",
        "components",
        "contribution_sum",
        "rounding_residual",
    )
    return [{field: window.get(field) for field in fields} for window in windows]


def write_detail_artifacts(
    output_dir: Path,
    engine: WorkbenchEngine,
    result: dict[str, Any],
) -> dict[str, str]:
    detail_dir = output_dir / "artifacts"
    detail_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "features": detail_dir / "features.json",
        "windows": detail_dir / "windows.json",
        "failures": detail_dir / "failures.json",
        "dependencies": detail_dir / "dependencies.json",
        "invalidation": detail_dir / "invalidation.json",
        "full_rebuild": detail_dir / "full_rebuild.json",
    }
    atomic_write_json(paths["features"], result.get("turns", []))
    atomic_write_json(paths["windows"], result.get("windows", []))
    atomic_write_json(paths["failures"], result.get("failures", []))
    atomic_write_json(paths["dependencies"], result.get("dependency_summary", {}))
    atomic_write_json(
        paths["invalidation"],
        {
            "batches": result.get("invalidation_history", []),
            "totals": aggregate_invalidation(result.get("invalidation_history", [])),
        },
    )
    if engine.full_rebuild_result is not None:
        atomic_write_json(paths["full_rebuild"], engine.full_rebuild_result)
    return {name: str(path) for name, path in paths.items() if path.exists()}


def build_report(
    *,
    engine: WorkbenchEngine,
    result: dict[str, Any],
    serial_result: dict[str, Any] | None,
    concurrency_comparison: dict[str, Any] | None,
    samples_dir: Path,
    artifact_paths: dict[str, str],
) -> dict[str, Any]:
    invalidation_history = result.get("invalidation_history", [])
    report = {
        "schema_version": 1,
        "report_type": "intent_drift_diagnostics",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "inputs": {
            "samples_dir": str(samples_dir),
            "events": str(samples_dir / "sessions.jsonl"),
            "config": str(samples_dir / "drift_config.json"),
            "update_plan": str(samples_dir / "update_plan.json"),
            "injections": str(samples_dir / "injections.json"),
        },
        "execution": result.get("execution"),
        "counts": result.get("counts"),
        "config": {
            "static_config_hash": engine.snapshot_manifest.get("config_hash")
            if engine.snapshot_manifest
            else None,
            "base_vocab_hash": engine.snapshot_manifest.get("base_vocab_hash")
            if engine.snapshot_manifest
            else None,
            "runtime_vocab": result.get("vocab"),
            "weights": result.get("weights"),
            "threshold": result.get("threshold"),
        },
        "snapshot": result.get("snapshot"),
        "recovery_point": result.get("recovery"),
        "equivalence": result.get("equivalence"),
        "concurrency_comparison": concurrency_comparison
        if concurrency_comparison is not None
        else {
            "performed": False,
            "equal": None,
            "note": "Use --compare-concurrency to run serial and parallel pipelines.",
        },
        "invalidation": {
            "source_totals": aggregate_invalidation(invalidation_history),
            "batches": invalidation_history,
        },
        "boundary_alerts": result.get("boundary_alerts", []),
        "injection_hits": result.get("injection_hits", []),
        "failure_list": result.get("failures", []),
        "windows_summary": _window_rows(result.get("windows", [])),
        "dependency_summary": {
            "raw_term_count": result.get("dependency_summary", {}).get("raw_term_count"),
            "vocab_terms": len(
                result.get("dependency_summary", {}).get("vocab_term_refs", {})
            ),
            "windows_indexed": len(
                result.get("dependency_summary", {}).get("window_dependencies", {})
            ),
        },
        "artifact_paths": artifact_paths,
        "final_state_digest": sha256_json(
            {
                "turns": result.get("turns", []),
                "windows": result.get("windows", []),
                "failures": result.get("failures", []),
            }
        ),
    }
    if serial_result is not None:
        report["serial_execution"] = serial_result.get("execution")
        report["serial_equivalence"] = serial_result.get("equivalence")
    return report


def write_report(output_dir: Path, report: dict[str, Any]) -> Path:
    path = output_dir / "diagnostics_report.json"
    atomic_write_json(path, report)
    return path
