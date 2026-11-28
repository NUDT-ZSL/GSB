"""Command-line interface for the sparse kernel workbench."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .errors import WorkbenchError
from .pipeline import atomic_write_json, build_report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sparse-kernel-workbench",
        description=(
            "Normalize Matrix Market/CSR/CSC fragments, execute row-safe sparse "
            "matrix multiplication batches, and audit kernel recommendations."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("run", "Build the full report and write it atomically as JSON"),
        ("validate", "Build and validate the full report without writing a file"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument(
            "--base-dir",
            default=".",
            help="Project base directory used for default input paths (default: cwd)",
        )
        command.add_argument(
            "--operation",
            default=None,
            help="Operation specification JSON (default: <base>/data/operation_spec.json)",
        )
        command.add_argument(
            "--version-spec",
            "--version",
            dest="version_specs",
            action="append",
            default=None,
            help="Version specification JSON; repeat for each version",
        )
        command.add_argument(
            "--direction",
            choices=["row_accumulate", "column_accumulate"],
            default=None,
            help="Optional override of the declared accumulation direction",
        )
        command.add_argument(
            "--batch-budget",
            type=int,
            default=None,
            help="Optional override of the declared per-batch nonzero budget",
        )
        command.add_argument(
            "--dense-chunk-size",
            type=int,
            default=None,
            help="Optional override of the dense-vector chunk size",
        )
        command.add_argument(
            "--hotspot-multiplier",
            default=None,
            help="Optional override of the declared hotspot multiplier",
        )
        command.add_argument(
            "--tolerance",
            type=float,
            default=None,
            help="Optional override of the declared absolute tolerance",
        )
        if name == "run":
            command.add_argument(
                "--output",
                default=None,
                help="Report path (default: <base>/reports/run_report.json)",
            )
    return parser


def _print_summary(report: dict, output: Path | None) -> None:
    status = report["status"]
    if status == "ok":
        recommendation = report["recommendation"]
        layout = recommendation["recommended_layout"]
        print(
            "status=ok "
            f"versions={len(report['versions'])} "
            f"recommended_layout={layout} "
            f"run_fingerprint={report['run_fingerprint'][:16]}"
        )
        if output is not None:
            print(f"report={output}")
    else:
        print(
            f"status=conflict versions={len(report['versions'])} "
            "recommendation=refused"
        )
        if output is not None:
            print(f"report={output}")
        for version in report["versions"]:
            if version.get("conflict"):
                print(
                    f"conflict version={version['version']} "
                    f"kind={version['conflict']['status']}"
                )
        if not report["cross_version"]["consistent"]:
            print(
                "cross_version_conflict=true "
                f"correction_count={report['cross_version']['correction_count']}"
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    base_dir = Path(args.base_dir).resolve()
    if args.operation:
        operation_path = Path(args.operation)
        if not operation_path.is_absolute():
            operation_path = base_dir / operation_path
        operation_path = operation_path.resolve()
    else:
        operation_path = base_dir / "data" / "operation_spec.json"
    try:
        report = build_report(
            operation_spec=operation_path,
            version_specs=args.version_specs,
            base_dir=base_dir,
            direction=args.direction,
            batch_budget=args.batch_budget,
            dense_chunk_size=args.dense_chunk_size,
            hotspot_multiplier=args.hotspot_multiplier,
            tolerance=args.tolerance,
        )
        output: Path | None = None
        if args.command == "run":
            if args.output:
                output = Path(args.output)
                if not output.is_absolute():
                    output = base_dir / output
                output = output.resolve()
            else:
                output = base_dir / "reports" / "run_report.json"
            atomic_write_json(output, report)
        _print_summary(report, output)
        return 0 if report["status"] == "ok" else 3
    except WorkbenchError as exc:
        json.dump(exc.to_dict(), sys.stderr, ensure_ascii=False, indent=2)
        sys.stderr.write("\n")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
