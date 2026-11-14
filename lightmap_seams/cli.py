"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .report import RESULT_FILENAME, STATUS_COMPLETED, run_analysis


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lightmap-seam-check",
        description=(
            "Detect light leaks across second-UV seams in baked lightmaps. "
            "Writes one <name>_seam_report.png per lightmap plus seam_result.json."
        ),
    )
    parser.add_argument(
        "--settings",
        required=True,
        type=Path,
        help="Path to the bake settings JSON.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Directory for annotated PNGs and seam_result.json.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with status 1 when any lightmap or the settings are incomplete.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_analysis(args.settings, args.output)

    summary = report["summary"]
    print(
        "lightmaps: {total_lightmaps} total, {completed_lightmaps} completed, "
        "{incomplete_lightmaps} incomplete; leaks: {total_leaks}".format(**summary)
    )
    for entry in report["lightmaps"]:
        line = f"  - {entry['name']}: {entry['status']}"
        if entry["status"] == STATUS_COMPLETED:
            line += (
                f" (seams={entry['total_seams']}, leaks={entry['leak_count']},"
                f" out_of_bounds={entry['out_of_bounds_count']},"
                f" duplicates={entry['duplicate_count']},"
                f" report={entry['report_file']})"
            )
        elif entry.get("reason"):
            line += f" - {entry['reason']}"
        print(line)
    print(f"result: {args.output / RESULT_FILENAME}")

    if args.strict and report["status"] != STATUS_COMPLETED:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
