"""Command-line entry point for SLPGuard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .batch import BatchConfig, run_batch
from .errors import ErrorCode, SLPError
from .models import JudgeConfig, ResourceLimits


def _optional_int(value: str) -> int | None:
    if value.lower() in {"none", "off", "unlimited"}:
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected an integer or 'none', got {value!r}") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _optional_float(value: str) -> float | None:
    if value.lower() in {"none", "off", "unlimited"}:
        return None
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected a number or 'none', got {value!r}") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slpguard",
        description="Offline SLP state-machine vulnerability verifier",
    )
    parser.add_argument("input", help="one .slp file or a directory (recursive)")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="batch worker count (default: 4)",
    )
    parser.add_argument(
        "--max-steps",
        type=_optional_int,
        default=1000,
        help="per-session packet step limit; integer or 'none' (default: 1000)",
    )
    parser.add_argument(
        "--max-seconds",
        type=_optional_float,
        default=15.0,
        help="real wall-clock limit per file; number or 'none' (default: 15)",
    )
    parser.add_argument(
        "--max-renegotiations",
        type=int,
        default=3,
        help="per-session renegotiation limit (default: 3)",
    )
    parser.add_argument("--workdir", default=".", help="workspace root (default: .)")
    parser.add_argument("--report", default=None, help="JSON report path")
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="ignore completed-file entries in the checkpoint",
    )
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="disable checkpoint reads and writes",
    )
    parser.add_argument(
        "--no-clean-temp",
        action="store_true",
        help="do not remove stale *.tmp files at startup",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="do not echo the JSON report to stdout",
    )
    return parser


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.max_renegotiations < 0:
        parser.error("--max-renegotiations must be non-negative")
    input_path = Path(args.input)
    if not input_path.exists():
        parser.error(f"input path does not exist: {input_path}")
    if not input_path.is_file() and not input_path.is_dir():
        parser.error(f"input is neither a file nor a directory: {input_path}")
    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        parser.error(f"workdir is not a directory: {workdir}")

    try:
        config = BatchConfig(
            workers=args.workers,
            limits=ResourceLimits(
                max_steps_per_session=args.max_steps,
                max_file_seconds=args.max_seconds,
            ),
            judge=JudgeConfig(max_renegotiations=args.max_renegotiations),
            resume=not args.no_resume,
            checkpoint_enabled=not args.no_checkpoint,
            clean_temp=not args.no_clean_temp,
            report_path=args.report,
        )
        report = run_batch(input_path, config=config, workdir=workdir)
    except SLPError as error:
        _json_print({"status": "error", "error": error.to_dict()})
        return 1
    except (OSError, ValueError) as error:
        _json_print(
            {
                "status": "error",
                "error": {
                    "code": ErrorCode.E_ARG.name,
                    "detail": str(error),
                },
            }
        )
        return 1

    if not args.quiet:
        _json_print(report)

    batch = report["batch"]
    business_failure = (
        batch["failed"] > 0
        or batch["limited"] > 0
        or batch.get("truth_mismatched_events", 0) > 0
        or batch.get("truth_unexpected_events", 0) > 0
        or bool(batch.get("checkpoint_errors"))
        or any(
            file.get("observed_violation_count", 0) > 0
            for file in report["files"]
        )
    )
    return 1 if business_failure else 0


if __name__ == "__main__":
    sys.exit(main())
