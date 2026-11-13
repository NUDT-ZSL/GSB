"""Command line interface: ``python -m wavmixer``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import run_mix


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wavmixer",
        description=(
            "Mix 16-bit PCM WAV clips on a millisecond timeline and write a "
            "JSON processing report."
        ),
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Path to the clip manifest JSON.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help=(
            "Path of the mixed WAV to write. The report is written next to it "
            "as '<out>.report.json'."
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional explicit report path (defaults to '<out>.report.json').",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print the report summary to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_mix(args.manifest, args.out, args.report)

    if not args.quiet:
        summary = {
            "status": report["status"],
            "reason": report["reason"],
            "output": report["output"],
            "total_duration_ms": report["total_duration_ms"],
            "overall_clipped": report["overall_clipped"],
            "clips_mixed": report["summary"]["mixed"],
            "clips_requested": report["summary"]["requested"],
            "excluded": [item["source"] for item in report["excluded_sources"]],
            "report": report.get("_report_path"),
        }
        if report["errors"]:
            summary["errors"] = [
                {"field": error.get("field"), "message": error.get("message")}
                for error in report["errors"]
            ]
        if report["format_mismatches"]:
            summary["format_mismatches"] = [
                {
                    "source": item["source"],
                    "sample_rate": item["sample_rate"],
                    "bits_per_sample": item["bits_per_sample"],
                    "channels": item["channels"],
                }
                for item in report["format_mismatches"]
            ]
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    return int(report.get("_exit_code", 4))


if __name__ == "__main__":
    sys.exit(main())
