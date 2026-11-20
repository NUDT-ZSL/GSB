"""Command-line entry points."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .demo import build_demo_report
from .models import ExecutionOptions, ResourceLimits, ToleranceConfig
from .snapshot import SnapshotStore
from .util import canonical_json_text, pretty_json_bytes


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m blockops",
        description="Deterministic blocked Softmax/LayerNorm operator demo",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run the complete deterministic demo")
    demo.add_argument("--data-dir", default="samples")
    demo.add_argument("--output-dir", default="build/demo")
    demo.add_argument("--abs-tol", type=_nonnegative_float, default=1e-12)
    demo.add_argument("--rel-tol", type=_nonnegative_float, default=1e-12)
    demo.add_argument("--max-blocks", type=_positive_int, default=100_000)
    demo.add_argument("--max-block-elements", type=_positive_int, default=1_000_000)
    demo.add_argument("--max-stat-bytes", type=_positive_int, default=64 * 1024 * 1024)
    demo.add_argument("--eps-floor", type=float, default=1e-5)

    query = subparsers.add_parser(
        "snapshot-query", help="query versioned snapshots from a JSON file"
    )
    query.add_argument("--file", required=True)
    query.add_argument("--category")

    return parser


def _run_demo(args: argparse.Namespace) -> int:
    options = ExecutionOptions(
        tolerance=ToleranceConfig(abs_tol=args.abs_tol, rel_tol=args.rel_tol),
        limits=ResourceLimits(
            max_blocks=args.max_blocks,
            max_block_elements=args.max_block_elements,
            max_stat_bytes=args.max_stat_bytes,
        ),
        eps_floor=args.eps_floor,
    )
    report = build_demo_report(args.data_dir, args.output_dir, options)
    summary = {
        "report": str(Path(args.output_dir) / "demo_report.json"),
        "report_core_sha256": report["report_core_sha256"],
        "verification_passed": report["verification"]["passed"],
        "rerun_byte_exact": report["verification"]["rerun_byte_exact"],
        "four_injection_stages": report["verification"][
            "four_injection_stages_seen"
        ],
        "business_codes": sorted(
            {
                code
                for job in report["jobs"]
                for code in job["business_codes"]
            }
            | {
                code
                for item in report["validation_probes"]
                for code in item["business_codes"]
            }
            | {
                code
                for item in report["resource_checks"]
                for code in item["business_codes"]
            }
        ),
    }
    sys.stdout.write(canonical_json_text(summary) + "\n")
    return 0 if report["verification"]["passed"] else 1


def _run_snapshot_query(args: argparse.Namespace) -> int:
    store, error = SnapshotStore.load(args.file)
    if error is not None:
        sys.stdout.buffer.write(pretty_json_bytes({"ok": False, "error": error}))
        return 2
    if args.category:
        payload = store.query(args.category)
        ok = bool(payload.get("ok"))
    else:
        payload = store.to_dict()
        ok = True
    sys.stdout.buffer.write(pretty_json_bytes(payload))
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            return _run_demo(args)
        if args.command == "snapshot-query":
            return _run_snapshot_query(args)
        parser.error("unknown command")
    except Exception as exc:
        payload = {"ok": False, "error_type": type(exc).__name__, "message": str(exc)}
        sys.stderr.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return 2
    return 2
