"""Local command-line examples for deterministic generation and validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from .bias import REFERENCE_ROUTE
from .decision import (
    DEFAULT_RESOURCE_PROFILE,
    ROUTE_PRIORITY,
    SCHEMA_VERSION,
    canonical_bytes,
)
from .generator import demo_tasks, read_jsonl, write_jsonl
from .injection import STAGES
from .pipeline import run_task
from .sampling import CDF_EQUALITY_RULE, TOP_P_BOUNDARY_RULE
from .snapshots import SnapshotStore


def _json_load_argument(value: str, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise SystemExit(f"{label} is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise SystemExit(f"{label} must be a JSON object")
    return parsed


def _load_extra_injections(args: argparse.Namespace) -> list[dict[str, Any]]:
    injections: list[dict[str, Any]] = []
    for value in args.inject or []:
        injections.append(_json_load_argument(value, label="--inject"))
    if getattr(args, "injection_file", None):
        path = Path(args.injection_file)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SystemExit(f"cannot read injection file {path}: {error}") from error
        if isinstance(loaded, dict):
            loaded = loaded.get("injections", [loaded])
        if not isinstance(loaded, list):
            raise SystemExit("injection file must contain an object or a list")
        for item in loaded:
            if not isinstance(item, dict):
                raise SystemExit("every injection entry must be an object")
            injections.append(item)
    return injections


def _manifest_digest(rows: list[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_bytes(rows)).hexdigest()


def _config_snapshot_content() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "reference_route": REFERENCE_ROUTE,
        "reference_dtype": "float64",
        "tie_policy": "score_desc_index_asc",
        "top_p_boundary_rule": TOP_P_BOUNDARY_RULE,
        "cdf_equality_rule": CDF_EQUALITY_RULE,
        "injection_stages": list(STAGES),
        "route_priority": ROUTE_PRIORITY,
        "default_resource_profile": dict(DEFAULT_RESOURCE_PROFILE),
    }


def _summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(case["status"] for case in cases)
    business_codes = sorted(
        {case["business_code"] for case in cases if case["business_code"] != "OK"}
    )
    routes = Counter(
        case["decision"]["selected_route"]
        for case in cases
        if case.get("decision") is not None
    )
    route_estimates: dict[str, int] = {}
    for case in cases:
        decision = case.get("decision")
        if not decision:
            continue
        for route, estimate in decision["estimates"].items():
            route_estimates[route] = route_estimates.get(route, 0) + int(estimate["comparisons"])
    bias_statuses = Counter(
        case["bias"].get("status", "missing")
        for case in cases
        if isinstance(case.get("bias"), dict)
    )
    selfcheck_statuses = Counter(
        case["self_check"].get("status", "missing")
        for case in cases
        if isinstance(case.get("self_check"), dict)
    )
    injection_stage_counts = Counter()
    injection_error_counts = Counter()
    for case in cases:
        for record in case.get("injections", []):
            injection_stage_counts[str(record.get("stage"))] += 1
            if record.get("business_code"):
                injection_error_counts[str(record.get("business_code"))] += 1
    max_deviation = max(
        (
            float(case["bias"]["max_abs_score_deviation"])
            for case in cases
            if isinstance(case.get("bias"), dict)
            and isinstance(case["bias"].get("max_abs_score_deviation"), (int, float))
        ),
        default=0.0,
    )
    non_injected_error_cases = [
        case["case_id"]
        for case in cases
        if case["status"] == "ERROR"
        or any(not error.get("injected", False) for error in case.get("errors", []))
    ]
    return {
        "case_count": len(cases),
        "status_counts": dict(sorted(status_counts.items())),
        "business_codes": business_codes,
        "selected_route_counts": dict(sorted(routes.items())),
        "route_comparison_estimates": dict(sorted(route_estimates.items())),
        "bias_status_counts": dict(sorted(bias_statuses.items())),
        "self_check_status_counts": dict(sorted(selfcheck_statuses.items())),
        "injection_stage_counts": dict(sorted(injection_stage_counts.items())),
        "injection_error_counts": dict(sorted(injection_error_counts.items())),
        "max_abs_score_deviation": max_deviation,
        "non_injected_error_cases": non_injected_error_cases,
    }


def _stats_snapshot_content(summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "case_count": summary["case_count"],
        "status_counts": summary["status_counts"],
        "business_codes": summary["business_codes"],
        "selected_route_counts": summary["selected_route_counts"],
        "route_comparison_estimates": summary["route_comparison_estimates"],
        "bias_status_counts": summary["bias_status_counts"],
        "self_check_status_counts": summary["self_check_status_counts"],
        "injection_stage_counts": summary["injection_stage_counts"],
        "injection_error_counts": summary["injection_error_counts"],
        "max_abs_score_deviation": summary["max_abs_score_deviation"],
        "non_injected_error_cases": summary["non_injected_error_cases"],
    }


def _write_json(path: str | Path, value: Any, *, pretty: bool) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        if pretty:
            json.dump(
                value,
                handle,
                sort_keys=True,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
        else:
            json.dump(
                value,
                handle,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        handle.write("\n")


def _run_rows(
    rows: list[dict[str, Any]],
    *,
    manifest_path: str,
    cache_dir: str,
    run_all_routes: bool,
    extra_injections: list[dict[str, Any]],
) -> dict[str, Any]:
    cases = [
        run_task(
            row,
            run_all_routes=run_all_routes,
            extra_injections=extra_injections,
            fallback_case_id=f"task-{index + 1}",
        )
        for index, row in enumerate(rows)
    ]
    summary = _summarize(cases)
    store = SnapshotStore(cache_dir)
    config_record = store.put("config", _config_snapshot_content())
    stats_record = store.put("stats", _stats_snapshot_content(summary))
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "run",
        "manifest": {
            "path": manifest_path,
            "case_count": len(rows),
            "sha256": _manifest_digest(rows),
        },
        "summary": summary,
        "snapshots": {
            "config": SnapshotStore.metadata(config_record),
            "stats": SnapshotStore.metadata(stats_record),
        },
        "cases": cases,
    }


def command_generate(args: argparse.Namespace) -> int:
    rows = demo_tasks()
    write_jsonl(args.out, rows)
    print(
        json.dumps(
            {
                "out": str(args.out),
                "case_count": len(rows),
                "sha256": _manifest_digest(rows),
                "case_ids": [row["case_id"] for row in rows],
            },
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    rows = read_jsonl(args.tasks)
    extra = _load_extra_injections(args)
    payload = _run_rows(
        rows,
        manifest_path=str(args.tasks),
        cache_dir=args.cache_dir,
        run_all_routes=not args.selected_only,
        extra_injections=extra,
    )
    _write_json(args.out, payload, pretty=args.pretty)
    summary = payload["summary"]
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False))
    if summary["non_injected_error_cases"] and not args.allow_errors:
        return 1
    return 0


def command_demo(args: argparse.Namespace) -> int:
    rows = demo_tasks()
    tasks_path = Path(args.tasks_out)
    write_jsonl(tasks_path, rows)
    extra: list[dict[str, Any]] = []
    payload = _run_rows(
        rows,
        manifest_path=str(tasks_path),
        cache_dir=args.cache_dir,
        run_all_routes=True,
        extra_injections=extra,
    )
    payload["command"] = "demo"
    payload["generated_tasks"] = {
        "path": str(tasks_path),
        "case_count": len(rows),
        "sha256": _manifest_digest(rows),
    }
    _write_json(args.out, payload, pretty=args.pretty)
    summary = payload["summary"]
    print(
        json.dumps(
            {
                "output": str(args.out),
                "tasks": str(tasks_path),
                **summary,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    if summary["non_injected_error_cases"] and not args.allow_errors:
        return 1
    return 0


def command_selfcheck(args: argparse.Namespace) -> int:
    rows = read_jsonl(args.tasks)
    extra = _load_extra_injections(args)
    payload = _run_rows(
        rows,
        manifest_path=str(args.tasks),
        cache_dir=args.cache_dir,
        run_all_routes=True,
        extra_injections=extra,
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "command": "selfcheck",
        "manifest": payload["manifest"],
        "summary": payload["summary"],
        "snapshots": payload["snapshots"],
        "cases": [
            {
                "case_id": case["case_id"],
                "business_code": case["business_code"],
                "decision_fingerprint": (
                    case["decision"]["canonical_bytes_sha256"]
                    if case.get("decision")
                    else None
                ),
                "self_check": case.get("self_check"),
                "bias": {
                    "status": case["bias"].get("status"),
                    "business_code": case["bias"].get("business_code"),
                    "max_abs_score_deviation": case["bias"].get("max_abs_score_deviation"),
                }
                if isinstance(case.get("bias"), dict)
                else None,
            }
            for case in payload["cases"]
        ],
    }
    _write_json(args.out, report, pretty=args.pretty)
    print(json.dumps(report["summary"], sort_keys=True, ensure_ascii=False))
    if report["summary"]["non_injected_error_cases"] and not args.allow_errors:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sampling-ops",
        description="Deterministic local TopK + sampling operator examples",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="write the deterministic demo JSONL manifest")
    generate.add_argument("--out", default="artifacts/tasks.jsonl")
    generate.set_defaults(handler=command_generate)

    run = subparsers.add_parser("run", help="run a JSONL manifest and write structured JSON")
    run.add_argument("--tasks", required=True)
    run.add_argument("--out", default="artifacts/run_output.json")
    run.add_argument("--cache-dir", default="artifacts/cache")
    run.add_argument("--pretty", action="store_true")
    run.add_argument("--selected-only", action="store_true", help="skip cross-route verification")
    run.add_argument(
        "--inject",
        action="append",
        help='JSON object injection, e.g. \'{"stage":"sample","type":"seed_missing"}\'',
    )
    run.add_argument("--injection-file")
    run.add_argument("--allow-errors", action="store_true")
    run.set_defaults(handler=command_run)

    demo = subparsers.add_parser("demo", help="generate and run the complete local demo")
    demo.add_argument("--out", default="artifacts/demo_output.json")
    demo.add_argument("--tasks-out", default="artifacts/tasks.jsonl")
    demo.add_argument("--cache-dir", default="artifacts/cache")
    demo.add_argument("--pretty", action="store_true")
    demo.add_argument("--allow-errors", action="store_true")
    demo.set_defaults(handler=command_demo)

    check = subparsers.add_parser("selfcheck", help="run manifests and emit compact self-check output")
    check.add_argument("--tasks", required=True)
    check.add_argument("--out", default="artifacts/selfcheck_output.json")
    check.add_argument("--cache-dir", default="artifacts/cache")
    check.add_argument("--pretty", action="store_true")
    check.add_argument("--inject", action="append")
    check.add_argument("--injection-file")
    check.add_argument("--allow-errors", action="store_true")
    check.set_defaults(handler=command_selfcheck)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

