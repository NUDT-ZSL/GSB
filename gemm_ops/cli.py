"""Command-line entry point for the complete local run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .config import (
    DEFAULT_COST_MODEL,
    DEFAULT_INJECTIONS,
    DEFAULT_REPORT,
    DEFAULT_RUNTIME,
    DEFAULT_SHAPE_SPECS,
    DEFAULT_STRATEGY_TABLE,
)
from .errors import GemmError
from .runner import run_suite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run adaptive GEMM/BMM benchmarks, accuracy checks and injections."
    )
    parser.add_argument("--output", default=str(DEFAULT_REPORT), help="JSON report path")
    parser.add_argument("--max-workers", type=int, default=None, help="runtime worker override")
    parser.add_argument("--shape-specs", default=str(DEFAULT_SHAPE_SPECS))
    parser.add_argument("--injections", default=str(DEFAULT_INJECTIONS))
    parser.add_argument("--strategy-table", default=str(DEFAULT_STRATEGY_TABLE))
    parser.add_argument("--cost-model", default=str(DEFAULT_COST_MODEL))
    parser.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_suite(
            shape_specs_path=args.shape_specs,
            injection_config_path=args.injections,
            output_path=args.output,
            max_workers=args.max_workers,
            strategy_table_path=args.strategy_table,
            cost_model_path=args.cost_model,
            runtime_path=args.runtime,
        )
    except GemmError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    summary = {
        "report": str(Path(args.output)),
        "passed": report["validation_summary"]["passed"],
        "shape_count": len(report["shapes"]),
        "cache": {
            key: report["cache"][key]
            for key in ("cache_hits", "cache_misses", "cache_invalidations", "cache_entries")
        },
        "injections": {
            "matched": report["injection"]["matched_count"],
            "total": report["injection"]["total"],
            "hit_injection_ids": report["injection"]["hit_injection_ids"],
        },
        "failures": report["failure_list"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["validation_summary"]["passed"] else 1
