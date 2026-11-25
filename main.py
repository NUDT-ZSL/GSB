#!/usr/bin/env python3
"""Command-line entry point for generation, execution, and validation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from term_pipeline.comparison import compare_paths
from term_pipeline.errors import PipelineError
from term_pipeline.injection import (
    EXPECTED_CODES,
    budget_degradation_example,
    inject_error,
)
from term_pipeline.pipeline import run_pipeline
from term_pipeline.runtime import load_committed_runtime
from term_pipeline.util import read_json, write_json_atomic
from term_pipeline.verification import verify_artifacts
from tools.generate_samples import generate


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _pipeline_error(exc: PipelineError) -> int:
    _print(exc.to_dict())
    return 2


def _internal_error(exc: Exception) -> int:
    _print(
        {
            "ok": False,
            "error_code": "E_INTERNAL",
            "message": str(exc),
            "details": {"type": type(exc).__name__},
        }
    )
    return 1


def _cmd_generate(args: argparse.Namespace) -> int:
    manifest = generate(Path(args.output))
    _print({"ok": True, **manifest})
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    result = run_pipeline(
        args.config,
        crash_after_stage=args.crash_after_stage,
    )
    _print(result)
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    run_pipeline(args.config)
    runtime = load_committed_runtime(args.config)
    report = compare_paths(
        runtime["documents"],
        runtime["scored_documents"],
        runtime["glossary"],
        runtime["rules"],
        runtime["features"],
        runtime["path_decision"],
        runtime["config"],
        strict=args.strict,
    )
    out_dir = Path(runtime["config"]["_resolved_paths"]["out_dir"])
    write_json_atomic(out_dir / "path_comparison.json", report)
    _print(
        {
            "ok": True,
            "adjudication": report["adjudication"],
            "index_stats_comparable": report["index_stats_comparable"],
            "coverage_recall_estimate": report["coverage_recall_estimate"],
            "difference_classes": report["difference_classes"],
            "term_set_diff": report["term_set_diff"],
            "sense_set_diff": report["sense_set_diff"],
            "max_candidate_confidence_delta": report[
                "max_candidate_confidence_delta"
            ],
            "report": str(out_dir / "path_comparison.json"),
        }
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    run_pipeline(args.config)
    runtime = load_committed_runtime(args.config)
    if len(runtime["snapshots"]) < 5:
        raise PipelineError(
            "E_INDEX_INCONSISTENT",
            "export stage has not been committed",
            {"committed_stages": len(runtime["snapshots"])},
        )
    out_dir = Path(runtime["config"]["_resolved_paths"]["out_dir"])
    index = read_json(out_dir / "term_index.json")
    result = verify_artifacts(
        index,
        out_dir / "ledger.sqlite3",
        runtime["documents"],
        runtime["scored_documents"],
        runtime["disambiguated_documents"],
        runtime["config"],
    )
    _print({"ok": True, "verification": result})
    return 0


def _cmd_budget_plan(args: argparse.Namespace) -> int:
    if args.policy == "report":
        plan = budget_degradation_example(
            Path(args.config).resolve().parent,
            total_compare_budget=args.total_compare_budget,
            max_resident_cache_mb=args.max_cache_mb,
        )
    else:
        run_pipeline(args.config)
        runtime = load_committed_runtime(args.config)
        config = runtime["config"]
        config["budget"]["total_compare_budget"] = args.total_compare_budget
        config["budget"]["max_resident_cache_mb"] = args.max_cache_mb
        from term_pipeline.budget import plan_admission

        plan = plan_admission(
            runtime["documents"],
            runtime["scored_documents"],
            runtime["features"],
            config,
            policy=args.policy,
        )
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out_dir / "budget_plan.json", plan)
    _print({"ok": True, "plan": plan, "report": str(out_dir / "budget_plan.json")})
    return 0


def _cmd_inject(args: argparse.Namespace) -> int:
    result = inject_error(args.case, args.root)
    _print(result)
    return 0 if result["ok"] else 2


def _cmd_inject_all(args: argparse.Namespace) -> int:
    results = [inject_error(case, args.root) for case in EXPECTED_CODES]
    summary = {
        "ok": all(item["ok"] for item in results),
        "case_count": len(results),
        "passed": sum(1 for item in results if item["ok"]),
        "results": results,
    }
    _print(summary)
    return 0 if summary["ok"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("--output", default=".")
    generate_parser.set_defaults(func=_cmd_generate)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", default="config.json")
    run_parser.add_argument(
        "--crash-after-stage",
        type=int,
        choices=(1, 2, 3, 4, 5),
        default=None,
        help="SIGKILL this process immediately after the selected stage commit",
    )
    run_parser.set_defaults(func=_cmd_run)

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--config", default="config.json")
    compare_parser.add_argument("--strict", action="store_true")
    compare_parser.set_defaults(func=_cmd_compare)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--config", default="config.json")
    verify_parser.set_defaults(func=_cmd_verify)

    budget_parser = subparsers.add_parser("budget-plan")
    budget_parser.add_argument("--config", default="config.json")
    budget_parser.add_argument("--policy", choices=("strict", "report"), default="report")
    budget_parser.add_argument("--total-compare-budget", type=int, default=500)
    budget_parser.add_argument("--max-cache-mb", type=float, default=128.0)
    budget_parser.add_argument("--output-dir", default="out")
    budget_parser.set_defaults(func=_cmd_budget_plan)

    inject_parser = subparsers.add_parser("inject-error")
    inject_parser.add_argument("--case", choices=tuple(EXPECTED_CODES), required=True)
    inject_parser.add_argument("--root", default=".")
    inject_parser.set_defaults(func=_cmd_inject)

    inject_all_parser = subparsers.add_parser("inject-all")
    inject_all_parser.add_argument("--root", default=".")
    inject_all_parser.set_defaults(func=_cmd_inject_all)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args(["run"] if len(sys.argv) == 1 else None)
    try:
        return int(args.func(args))
    except PipelineError as exc:
        return _pipeline_error(exc)
    except Exception as exc:  # pragma: no cover - diagnostic path
        return _internal_error(exc)


if __name__ == "__main__":
    sys.exit(main())

