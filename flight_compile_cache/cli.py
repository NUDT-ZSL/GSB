"""Command-line entry point for the deterministic compilation-cache demo."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .cache import CompileCacheEngine
from .generator import DEFAULT_SEED, generate_bundle
from .util import atomic_write_json, read_json
from .validation import validate_project
from .verification import run_edge_project, run_project


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def cmd_generate(args: argparse.Namespace) -> int:
    bundle = generate_bundle(args.out_dir, seed=args.seed)
    summary = {
        "out_dir": bundle["out_dir"],
        "project": str(bundle["project"]),
        "edge_project": str(bundle["edge_project"]),
        "manifest": str(bundle["manifest"]),
        "counts": bundle["manifest_data"]["counts"],
        "project_fingerprint": bundle["manifest_data"]["project_fingerprint"],
    }
    _print(summary)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    project = read_json(args.project)
    report = validate_project(project)
    result = {
        "project": str(args.project),
        "ok": report["ok"],
        "executable": report["executable"],
        "project_errors": report["errors"],
        "invalid_models": [
            {
                "model_id": model_id,
                "errors": item["errors"],
            }
            for model_id, item in report["models"].items()
            if not item["ok"]
        ],
        "invalid_requests": [
            {
                "request_id": request_id,
                "error_code": item.get("error_code"),
                "errors": item["errors"],
            }
            for request_id, item in report["requests"].items()
            if not item["ok"]
        ],
        "invalid_events": [
            {"event_id": event_id, "errors": item["errors"]}
            for event_id, item in report["events"].items()
            if not item["ok"]
        ],
    }
    if args.out:
        atomic_write_json(args.out, report)
        result["report_path"] = str(args.out)
    _print(result)
    return 0 if report["executable"] else 1


def cmd_run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    if args.project:
        project = read_json(args.project)
        project_path = Path(args.project)
    else:
        generated = generate_bundle(out_dir / "generated", seed=args.seed)
        project = generated["project_data"]
        project_path = generated["project"]
    report = run_project(
        project,
        out_dir=out_dir,
        cutoff_index=args.cutoff_index,
        write_snapshots=True,
    )
    summary = {
        "project": str(project_path),
        "report": str(out_dir / "report.json"),
        "request_log": str(out_dir / "request_log.jsonl"),
        "checkpoint": str(out_dir / "cache_checkpoint.json"),
        "final_snapshot": str(out_dir / "cache_final.json"),
        "latest_snapshot": str(out_dir / "latest_snapshot.json"),
        "overall_ok": report["overall_ok"],
        "valid_requests": report["summary"]["valid_requests"],
        "rejected_requests": report["summary"]["rejected_requests"],
        "cache_hits": report["summary"]["cache_hits"],
        "cache_misses": report["summary"]["cache_misses"],
        "hit_rate": report["summary"]["hit_rate"],
        "evictions": report["summary"]["eviction_count"],
        "shape_specialization_events": report["shape_specialization"]["events"],
        "event_incremental_compiles": report["shape_specialization"][
            "event_incremental_compiles"
        ],
        "baseline_work": report["full_compile_baseline"]["compile_work"],
        "specialized_work": report["full_compile_baseline"]["specialized_compile_work"],
        "work_reduction_ratio": report["full_compile_baseline"]["work_reduction_ratio"],
        "numeric_mismatches": len(report["full_compile_baseline"]["numeric_mismatches"]),
        "restart_hit_decisions_identical": report["restart_verification"][
            "hit_decisions_identical"
        ],
        "acceptance_checks": report["acceptance_checks"],
    }
    _print(summary)
    return 0 if report["overall_ok"] else 1


def cmd_edge(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    if args.project:
        project = read_json(args.project)
    else:
        generated = generate_bundle(out_dir / "generated", seed=args.seed)
        project = generated["edge_project_data"]
    report = run_edge_project(project, out_dir=out_dir)
    _print(
        {
            "edge_report": str(out_dir / "edge_report.json"),
            "all_expected_outcomes_match": report["all_expected_outcomes_match"],
            "model_error_codes": report["model_error_codes"],
            "requests": [
                {
                    "request_id": item["request_id"],
                    "status": item["status"],
                    "error_code": item.get("error_code"),
                }
                for item in report["requests"]
            ],
        }
    )
    return 0 if report["all_expected_outcomes_match"] else 1


def cmd_snapshot_check(args: argparse.Namespace) -> int:
    project = read_json(args.project)
    engine = CompileCacheEngine.restore(args.snapshot, project)
    result = {
        "snapshot": str(args.snapshot),
        "project": str(args.project),
        "restore_report": engine.restore_report,
        "cache_size": len(engine.entries),
        "artifact_count": len(engine.artifact_registry),
        "current_versions": engine.current_versions,
        "summary": engine.summary(),
    }
    if args.out:
        atomic_write_json(args.out, result)
        result["output_path"] = str(args.out)
    _print(result)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    out = Path(args.out_dir)
    generated = generate_bundle(out / "generated", seed=args.seed)
    run_report = run_project(
        generated["project_data"],
        out_dir=out / "run",
        write_snapshots=True,
    )
    edge_report = run_edge_project(generated["edge_project_data"], out_dir=out / "edge")
    summary = {
        "out_dir": str(out),
        "generated_project": str(generated["project"]),
        "manifest": str(generated["manifest"]),
        "run_report": str(out / "run" / "report.json"),
        "edge_report": str(out / "edge" / "edge_report.json"),
        "overall_ok": run_report["overall_ok"] and edge_report["all_expected_outcomes_match"],
        "run": {
            "valid_requests": run_report["summary"]["valid_requests"],
            "rejected_requests": run_report["summary"]["rejected_requests"],
            "hit_rate": run_report["summary"]["hit_rate"],
            "evictions": run_report["summary"]["eviction_count"],
            "shape_specialization_events": run_report["shape_specialization"]["events"],
            "work_reduction_ratio": run_report["full_compile_baseline"][
                "work_reduction_ratio"
            ],
            "restart_hit_decisions_identical": run_report["restart_verification"][
                "hit_decisions_identical"
            ],
            "acceptance_checks": run_report["acceptance_checks"],
        },
        "edge": {
            "all_expected_outcomes_match": edge_report["all_expected_outcomes_match"],
            "model_error_codes": edge_report["model_error_codes"],
        },
    }
    _print(summary)
    return 0 if summary["overall_ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flight-compile-cache",
        description=(
            "Deterministic airline flight-scheduling inference compile cache with "
            "shape-specialized artifacts and a full-compile numeric baseline."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    generate = sub.add_parser("generate", help="Generate deterministic project fixtures")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.add_argument("--out-dir", default="artifacts/generated")
    generate.set_defaults(func=cmd_generate)

    validate = sub.add_parser("validate", help="Validate a project and print business error codes")
    validate.add_argument("--project", required=True)
    validate.add_argument("--out")
    validate.set_defaults(func=cmd_validate)

    run = sub.add_parser("run", help="Run requests/events, compare baseline, and verify restart")
    run.add_argument("--project")
    run.add_argument("--seed", type=int, default=DEFAULT_SEED)
    run.add_argument("--out-dir", default="artifacts/run")
    run.add_argument("--cutoff-index", type=int, default=None)
    run.set_defaults(func=cmd_run)

    edge = sub.add_parser("edge", help="Run the malformed-graph edge-case fixture")
    edge.add_argument("--project")
    edge.add_argument("--seed", type=int, default=DEFAULT_SEED)
    edge.add_argument("--out-dir", default="artifacts/edge")
    edge.set_defaults(func=cmd_edge)

    snapshot = sub.add_parser("snapshot-check", help="Restore a snapshot and show version checks")
    snapshot.add_argument("--project", required=True)
    snapshot.add_argument("--snapshot", required=True)
    snapshot.add_argument("--out")
    snapshot.set_defaults(func=cmd_snapshot_check)

    demo = sub.add_parser("demo", help="Generate, run, verify restart, and run edge cases")
    demo.add_argument("--seed", type=int, default=DEFAULT_SEED)
    demo.add_argument("--out-dir", default="artifacts/demo")
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["demo", *list(argv or [])])
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
