from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .config import ConfigValidationError, load_config
from .errors import E_OUTPUT_INVALID, WorkbenchError
from .pipeline import WorkbenchEngine


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gbdt-workbench",
        description="Deterministic tabular residual-tree ensemble diagnostics workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run incremental training and diagnostics")
    run.add_argument("--config", required=True, help="experiment YAML path")
    run.add_argument(
        "--output",
        default="reports/latest",
        help="report and snapshot directory (default: reports/latest)",
    )
    run.add_argument(
        "--strict-restore",
        action="store_true",
        help="reject snapshots when the manifest config fingerprint differs",
    )
    run.add_argument(
        "--force-rebuild",
        action="store_true",
        help="ignore stage caches and recompute all stages",
    )

    rebuild = subparsers.add_parser(
        "rebuild", help="ignore snapshots and perform a full serial rebuild"
    )
    rebuild.add_argument("--config", required=True, help="experiment YAML path")
    rebuild.add_argument(
        "--output",
        default="reports/rebuild",
        help="report and snapshot directory (default: reports/rebuild)",
    )

    validate = subparsers.add_parser("validate", help="validate a YAML experiment config")
    validate.add_argument("--config", required=True, help="experiment YAML path")

    compare = subparsers.add_parser(
        "compare", help="compare two deterministic diagnostic reports"
    )
    compare.add_argument("--left", required=True, help="first diagnostic_report.json")
    compare.add_argument("--right", required=True, help="second diagnostic_report.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        try:
            loaded = load_config(args.config)
        except ConfigValidationError as exc:
            print(
                json.dumps(
                    {"status": "failed", "failures": [item.to_dict() for item in exc.failures]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_fingerprint": loaded.config_fingerprint,
                    "config": loaded.config.normalized(),
                    "data_spec": str(loaded.config.data_spec_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "compare":
        try:
            with open(args.left, "r", encoding="utf-8") as left_handle:
                left = json.load(left_handle)
            with open(args.right, "r", encoding="utf-8") as right_handle:
                right = json.load(right_handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"E_OUTPUT_INVALID: {exc}", file=sys.stderr)
            return 4
        equal = left == right
        print(
            json.dumps(
                {
                    "equal": equal,
                    "left_digest": left.get("equivalence_digest"),
                    "right_digest": right.get("equivalence_digest"),
                    "left_status": left.get("status"),
                    "right_status": right.get("status"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if equal else 1

    try:
        result = WorkbenchEngine(
            args.config,
            args.output,
            force_rebuild=bool(getattr(args, "force_rebuild", False))
            or args.command == "rebuild",
            strict_restore=bool(getattr(args, "strict_restore", False)),
        ).run()
    except WorkbenchError as exc:
        if exc.failure.code == E_OUTPUT_INVALID:
            print(f"E_OUTPUT_INVALID: {exc.failure.message}", file=sys.stderr)
            return 4
        print(f"{exc.failure.code}: {exc.failure.message}", file=sys.stderr)
        return 4
    summary = {
        "status": result.status,
        "exit_code": result.exit_code,
        "diagnostic_report": str(result.diagnostic_path),
        "run_report": str(result.run_report_path),
        "equivalence_digest": result.diagnostic_report.get("equivalence_digest"),
        "reused_tree_indices": result.run_report.get("operational_audit", {})
        .get("reused", {})
        .get("tree_indices", []),
        "computed_tree_indices": result.run_report.get("operational_audit", {})
        .get("computed", {})
        .get("tree_indices", []),
        "first_exceeding_stage": (
            result.diagnostic_report.get("error_attribution") or {}
        ).get("first_exceeding_stage"),
        "threshold_sensitive_samples": len(
            result.diagnostic_report.get("threshold_sensitive_samples", [])
        ),
        "failure_count": len(result.diagnostic_report.get("failures", [])),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return int(result.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
