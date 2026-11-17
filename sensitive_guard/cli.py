"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .batch import (
    BatchFormatError,
    BatchOptions,
    filter_sources,
    load_batch,
    run_batch,
)
from .io_utils import atomic_write_json


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sensitive_guard",
        description="Sensitive-marker derivation guard engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate one or more sets")
    validate.add_argument("--input", required=True)
    validate.add_argument("--report", required=True)
    validate.add_argument("--concurrency", type=int, default=4)
    validate.add_argument("--timeout-ms", type=int, default=2000)
    validate.add_argument("--max-fields", type=int, default=10000)
    validate.add_argument("--dataset-id", action="append", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.concurrency <= 0:
        parser.error("--concurrency must be greater than zero")
    if args.timeout_ms <= 0:
        parser.error("--timeout-ms must be greater than zero")
    if args.max_fields <= 0:
        parser.error("--max-fields must be greater than zero")

    try:
        sources = load_batch(args.input)
    except BatchFormatError as exc:
        print(f"E_ARGUMENT: {exc}", file=sys.stderr)
        return 2

    sources = filter_sources(sources, args.dataset_id)
    if not sources:
        print("E_ARGUMENT: no dataset matched --dataset-id", file=sys.stderr)
        return 2

    options = BatchOptions(
        concurrency=args.concurrency,
        timeout_ms=args.timeout_ms,
        max_fields=args.max_fields,
    )
    try:
        report = run_batch(
            sources,
            input_path=args.input,
            report_path=args.report,
            options=options,
        )
        atomic_write_json(args.report, report)
    except (OSError, ValueError) as exc:
        print(f"E_ARGUMENT: {exc}", file=sys.stderr)
        return 2

    exit_code = 0 if report["summary"]["all_success"] else 1
    print(
        json.dumps(
            {
                "report": str(Path(args.report).resolve()),
                "exit_code": exit_code,
                "summary": report["summary"],
            },
            ensure_ascii=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
