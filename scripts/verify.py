"""End-to-end deterministic verification used for acceptance evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sampling_ops.decision import decision_bytes
from sampling_ops.generator import demo_tasks, write_jsonl
from sampling_ops.pipeline import run_task
from sampling_ops.snapshots import SnapshotStore


def _digest(value: Any) -> str:
    data = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def check(name: str, condition: bool, details: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(condition), "details": details}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="artifacts/verification_summary.json")
    parser.add_argument("--tasks-out", default="artifacts/tasks.jsonl")
    parser.add_argument("--demo-out", default="artifacts/demo_output.json")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    rows = demo_tasks()
    tasks_path = Path(args.tasks_out)
    write_jsonl(tasks_path, rows)
    cases = [run_task(row) for row in rows]
    by_id = {case["case_id"]: case for case in cases}
    checks: list[dict[str, Any]] = []

    checks.append(
        check(
            "all_cross_route_candidates_equal",
            all(
                case.get("topk") is not None
                and case["topk"].get("cross_route_consistency") is not None
                and case["topk"]["cross_route_consistency"]["ok"]
                for case in cases
                if case["status"] == "OK"
            ),
            {
                "ok_cases": [
                    case["case_id"] for case in cases if case["status"] == "OK"
                ]
            },
        )
    )
    sample_consistency: dict[str, bool] = {}
    for case in cases:
        if case["status"] != "OK":
            continue
        route_samples = {
            route: payload["samples"]
            for route, payload in case["sampling"]["per_route"].items()
        }
        baseline = route_samples["full_sort"]
        sample_consistency[case["case_id"]] = all(
            value == baseline for value in route_samples.values()
        )
    checks.append(
        check(
            "all_cross_route_samples_equal",
            all(sample_consistency.values()),
            sample_consistency,
        )
    )

    decision_checks: dict[str, bool] = {}
    for row in rows[:4]:
        first = run_task(row)
        second = run_task(row)
        decision_checks[row["case_id"]] = (
            decision_bytes(first["decision"]) == decision_bytes(second["decision"])
            and first["decision"]["canonical_bytes_sha256"]
            == second["decision"]["canonical_bytes_sha256"]
        )
    checks.append(
        check("decision_bytes_reproducible", all(decision_checks.values()), decision_checks)
    )

    selected_routes = {
        case_id: case["decision"]["selected_route"]
        for case_id, case in by_id.items()
        if case.get("decision")
    }
    checks.append(
        check(
            "multi_scale_route_selection",
            {
                "small-full-sort": "full_sort",
                "compare-tight-heap": "heap_select",
                "memory-tight-chunked": "chunked_two_stage",
            }.items()
            <= selected_routes.items(),
            selected_routes,
        )
    )

    injection_checks: dict[str, Any] = {}
    for expected_stage, expected_code in (
        ("candidate_filter", "E_LOGITS_NON_FINITE"),
        ("temperature_scale", "E_TEMPERATURE_INVALID"),
        ("top_p_filter", "E_TOP_P_INVALID"),
        ("sample", "E_SEED_INVALID"),
    ):
        matching = [
            case
            for case in cases
            if any(
                trace["stage"] == expected_stage
                and trace["status"] == "error"
                and trace["business_code"] == expected_code
                and trace["injected"]
                for trace in case["stage_trace"]
            )
        ]
        injection_checks[expected_stage] = {
            "ok": len(matching) == 1,
            "case_id": matching[0]["case_id"] if matching else None,
            "business_code": expected_code,
        }
    checks.append(
        check(
            "four_stage_injection_attribution",
            all(item["ok"] for item in injection_checks.values()),
            injection_checks,
        )
    )

    overrun = by_id["resource-overrun-check"]
    checks.append(
        check(
            "resource_overrun_remains_auditable",
            (
                not overrun["decision"]["selected_within_limits"]
                and overrun["decision"]["degraded_category"] == "RESOURCE_BUDGET_OVERRUN"
                and overrun["topk"]["candidates"] is not None
                and overrun["sampling"]["samples"] is not None
                and overrun["bias"]["business_code"] == "OK"
            ),
            {
                "selected_route": overrun["decision"]["selected_route"],
                "exceeded_metrics": overrun["decision"]["selected_exceeded_metrics"],
                "warnings": overrun["warnings"],
            },
        )
    )

    bias_checks = {
        case["case_id"]: {
            "bias": case["bias"]["business_code"],
            "self_check": case["self_check"]["business_code"],
            "max_abs_score_deviation": case["bias"]["max_abs_score_deviation"],
        }
        for case in cases
        if case["status"] == "OK"
    }
    checks.append(
        check(
            "float64_reference_and_replay",
            all(
                item["bias"] == "OK"
                and item["self_check"] == "OK"
                and item["max_abs_score_deviation"] <= 1e-12
                for item in bias_checks.values()
            ),
            bias_checks,
        )
    )

    with tempfile.TemporaryDirectory() as directory:
        store = SnapshotStore(directory)
        first = store.put("config", {"policy": 1})
        same = store.put("config", {"policy": 1})
        changed = store.put("config", {"policy": 2})
        stats = store.put("stats", {"cases": len(cases)})
        snapshots_ok = (
            first["version"] == 1
            and same["version"] == 1
            and not same["changed"]
            and changed["version"] == 2
            and changed["content"] == {"policy": 2}
            and stats["version"] == 1
        )
        snapshot_details = {
            "config_versions": [first["version"], same["version"], changed["version"]],
            "stats_version": stats["version"],
            "changed_checksums": [first["checksum"], changed["checksum"]],
        }
    checks.append(check("snapshot_versioning", snapshots_ok, snapshot_details))

    summary = {
        "schema_version": "1.0",
        "environment": {
            "python": __import__("sys").version.split()[0],
            "numpy": np.__version__,
        },
        "inputs": {
            "tasks_path": str(tasks_path),
            "case_count": len(rows),
            "tasks_sha256": _digest(rows),
        },
        "cases": [
            {
                "case_id": case["case_id"],
                "status": case["status"],
                "business_code": case["business_code"],
                "selected_route": (
                    case["decision"]["selected_route"] if case.get("decision") else None
                ),
                "selected_within_limits": (
                    case["decision"]["selected_within_limits"]
                    if case.get("decision")
                    else None
                ),
                "degraded_category": (
                    case["decision"]["degraded_category"]
                    if case.get("decision")
                    else None
                ),
                "bias": (
                    case["bias"].get("business_code")
                    if isinstance(case.get("bias"), dict)
                    else None
                ),
                "self_check": (
                    case["self_check"].get("business_code")
                    if isinstance(case.get("self_check"), dict)
                    else None
                ),
                "max_abs_score_deviation": (
                    case["bias"].get("max_abs_score_deviation")
                    if isinstance(case.get("bias"), dict)
                    else None
                ),
            }
            for case in cases
        ],
        "checks": checks,
        "all_checks_ok": all(item["ok"] for item in checks),
    }
    output = {
        **summary,
        "case_summaries": summary["cases"],
        "cases": cases,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            output,
            handle,
            sort_keys=True,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            allow_nan=False,
        )
        handle.write("\n")
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False))
    return 0 if summary["all_checks_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

