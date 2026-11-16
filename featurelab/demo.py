"""Deterministic local demo and serial-equivalence verification."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .app import FeatureEngineeringApp
from .utils import atomic_write_json


DEFAULT_COMMANDS = (
    "evaluate",
    "set channel frequency",
    "set-param target.smoothing 4.0",
    "set-param selector.weight_elimination.max_eliminations 5",
    "evaluate",
    "status",
)


def run_sequence(
    config_path: str | Path,
    *,
    commands: Sequence[str] = DEFAULT_COMMANDS,
    state_dir: Optional[str | Path] = None,
    serial: bool = False,
    cache_enabled: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]], FeatureEngineeringApp]:
    temporary: Optional[tempfile.TemporaryDirectory[str]] = None
    if state_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="featurelab-demo-")
        state_dir = temporary.name
    app = FeatureEngineeringApp(
        config_path,
        serial=serial,
        cache_enabled=cache_enabled,
        state_dir=state_dir,
        resume=False,
    )
    responses: list[dict[str, Any]] = []
    try:
        for command in commands:
            responses.append(app.handle_command(command))
        app.wait_for_all()
        app.close(snapshot=True)
        report = app.build_report()
    finally:
        if not app._closed:
            app.close(snapshot=True)
        if temporary is not None:
            temporary.cleanup()
    return report, responses, app


def _comparison_projection(report: Mapping[str, Any]) -> dict[str, Any]:
    plans = []
    for plan in report.get("plans", []):
        plans.append(
            {
                "plan_id": plan.get("plan_id"),
                "plan": plan.get("plan"),
                "status": plan.get("status"),
                "metrics": plan.get("metrics"),
                "fold_metrics": plan.get("fold_metrics"),
                "selected_features": plan.get("selected_features"),
                "removed_features": plan.get("removed_features"),
                "elimination_rounds": plan.get("elimination_rounds"),
                "encodings": plan.get("encodings"),
                "feature_names": plan.get("feature_names"),
                "rank": plan.get("rank"),
            }
        )
    return {
        "dataset": report.get("dataset"),
        "active_plan_id": report.get("active_plan_id"),
        "plans": plans,
        "ranking": report.get("ranking"),
        "best_plan_id": (report.get("best_plan") or {}).get("plan_id"),
        "semantic_fingerprint": report.get("semantic_fingerprint"),
        "commands": [
            {
                "sequence": item.get("sequence"),
                "command": item.get("command"),
                "ok": item.get("ok"),
                "code": item.get("code"),
                "current_plan_id": item.get("current_plan_id"),
                "best_plan_id": item.get("best_plan_id"),
            }
            for item in report.get("commands", [])
        ],
    }


def _cache_headline(cache: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: cache.get(key)
        for key in (
            "hits",
            "misses",
            "invalidations",
            "computes",
            "bypasses",
            "invalidated_feature_columns",
            "invalidated_feature_count",
        )
    }


def verify_equivalence(
    config_path: str | Path,
    *,
    commands: Sequence[str] = DEFAULT_COMMANDS,
    equivalence_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    incremental, incremental_responses, _ = run_sequence(
        config_path,
        commands=commands,
        serial=False,
        cache_enabled=True,
    )
    serial, serial_responses, _ = run_sequence(
        config_path,
        commands=commands,
        serial=True,
        cache_enabled=False,
    )
    left = _comparison_projection(incremental)
    right = _comparison_projection(serial)
    equivalent = left == right
    result = {
        "equivalent": equivalent,
        "incremental_semantic_fingerprint": incremental.get("semantic_fingerprint"),
        "serial_semantic_fingerprint": serial.get("semantic_fingerprint"),
        "incremental_ranking": incremental.get("ranking"),
        "serial_ranking": serial.get("ranking"),
        "incremental_best_plan": (incremental.get("best_plan") or {}).get("plan_id"),
        "serial_best_plan": (serial.get("best_plan") or {}).get("plan_id"),
        "incremental_cache": _cache_headline(incremental.get("cache", {})),
        "serial_cache": _cache_headline(serial.get("cache", {})),
        "incremental_command_count": len(incremental_responses),
        "serial_command_count": len(serial_responses),
        "timing_fields_excluded": ["commands[].duration_ms", "commands[].within_budget"],
        "cache_fields_excluded": ["cache", "snapshot_version", "snapshot_dir", "recovered"],
    }
    if not equivalent:
        result["differences"] = _diff(left, right, "$")
    return result


def _diff(left: Any, right: Any, path: str) -> list[dict[str, Any]]:
    if type(left) is not type(right):
        return [{"path": path, "incremental": left, "serial": right}]
    if isinstance(left, Mapping):
        differences: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                differences.append(
                    {
                        "path": f"{path}.{key}",
                        "incremental": left.get(key),
                        "serial": right.get(key),
                    }
                )
            else:
                differences.extend(_diff(left[key], right[key], f"{path}.{key}"))
        return differences
    if isinstance(left, list):
        if len(left) != len(right):
            return [{"path": path, "incremental_length": len(left), "serial_length": len(right)}]
        differences = []
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            differences.extend(_diff(left_item, right_item, f"{path}[{index}]"))
        return differences
    if left != right:
        return [{"path": path, "incremental": left, "serial": right}]
    return []


def write_demo_outputs(
    config_path: str | Path,
    report_path: str | Path,
    *,
    verify: bool = False,
    equivalence_path: Optional[str | Path] = None,
    serial: bool = False,
    cache_enabled: bool = True,
) -> dict[str, Any]:
    report, responses, _ = run_sequence(
        config_path,
        serial=serial,
        cache_enabled=cache_enabled,
    )
    atomic_write_json(report_path, report)
    result: dict[str, Any] = {
        "report_path": str(Path(report_path)),
        "command_count": len(responses),
        "semantic_fingerprint": report["semantic_fingerprint"],
        "snapshot_version": report["snapshot_version"],
        "best_plan": (report.get("best_plan") or {}).get("plan_id"),
        "ranking": report.get("ranking"),
        "cache": _cache_headline(report.get("cache", {})),
    }
    if verify:
        verification = verify_equivalence(
            config_path,
            equivalence_path=equivalence_path,
        )
        if equivalence_path is not None:
            atomic_write_json(equivalence_path, verification)
        result["verification"] = verification
    return result
