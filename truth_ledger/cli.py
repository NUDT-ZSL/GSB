from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .adjudicate import adjudicate_from_disk
from .errors import LedgerError
from .hashing import write_json_atomic
from .injections import run_injection_suite
from .pipeline import Pipeline
from .sample import FIXED_SEED, generate_task_workspace
from .selfcheck import run_self_check


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="truth-ledger",
        description="Deterministic multi-format sparse matrix truth ledger",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate fixed-seed samples")
    generate.add_argument("--out", required=True)
    generate.add_argument("--cases", type=int, default=2)
    generate.add_argument("--seed", type=int, default=FIXED_SEED)

    pipeline = sub.add_parser("pipeline", help="run four-stage pipeline")
    pipeline.add_argument("--case-dir", required=True)
    pipeline.add_argument("--target-version", type=int, required=True)
    pipeline.add_argument("--crash-at")
    pipeline.add_argument("--injection")

    adjudicate = sub.add_parser("adjudicate", help="adjudicate three disk views")
    adjudicate.add_argument("--case-dir", required=True)
    adjudicate.add_argument("--output")

    self_check = sub.add_parser("self-check", help="run cross-module consistency check")
    self_check.add_argument("--case-dir", required=True)

    inject = sub.add_parser("inject", help="run exception injection suite")
    inject.add_argument("--out", required=True)

    demo = sub.add_parser("demo", help="run full local acceptance demonstration")
    demo.add_argument("--root", default="artifacts/demo")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            manifest = generate_task_workspace(
                args.out, case_count=args.cases, seed=args.seed
            )
            _print_json({"ok": True, "task_manifest": str(Path(args.out) / "task_manifest.json"), "case_count": manifest["case_count"]})
            return 0

        if args.command == "pipeline":
            result = Pipeline(args.case_dir).run_through(
                args.target_version,
                crash_at=args.crash_at,
                injection=args.injection,
            )
            _print_json(result)
            return 0

        if args.command == "adjudicate":
            report = adjudicate_from_disk(args.case_dir)
            if args.output:
                write_json_atomic(args.output, report)
            _print_json({key: value for key, value in report.items() if key not in ("views", "intervals")})
            return 0

        if args.command == "self-check":
            _print_json(run_self_check(args.case_dir))
            return 0

        if args.command == "inject":
            summary = run_injection_suite(args.out)
            _print_json(
                {
                    "ok": summary["ok"],
                    "total": summary["total"],
                    "passed_count": summary["passed_count"],
                    "failed_count": summary["failed_count"],
                    "records_path": str(Path(args.out) / "injection_records.json"),
                }
            )
            return 0 if summary["ok"] else 2

        if args.command == "demo":
            from .demo import run_demo

            result = run_demo(args.root)
            _print_json(
                {
                    "ok": True,
                    "report_path": result["report_path"],
                    "determinism": result["determinism"],
                    "normal_v2_recompute": result["normal_run"]["v2"]["recompute"],
                    "recovery_returncode": result["recovery"]["crash_returncode"],
                    "recovery_stages": result["recovery"]["result"]["stage_runs"],
                    "injections": result["injections"],
                }
            )
            return 0

        return 1
    except LedgerError as exc:
        _print_json(exc.to_record())
        return 2


if __name__ == "__main__":
    sys.exit(main())
