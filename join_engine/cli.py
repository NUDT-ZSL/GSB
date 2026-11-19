from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .engine import (
    benchmark_query,
    execute_query,
    plan_query,
    status_query,
    verify_query,
)
from .errors import EngineError, fail
from .models import json_dumps_stable, load_sample


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="join-engine",
        description="Deterministic local Shuffle + join execution engine",
    )
    parser.add_argument(
        "command",
        choices=("plan", "run", "resume", "status", "verify", "benchmark"),
        help="command to execute",
    )
    parser.add_argument(
        "--sample",
        default="samples/query_sample.json",
        help="path to the self-contained JSON sample",
    )
    parser.add_argument(
        "--state-dir",
        default=".runtime",
        help="state, partition and result directory",
    )
    parser.add_argument("--scale", help="scale sample name; defaults to execution.default_scale")
    parser.add_argument(
        "--partitions",
        help="comma-separated partition ids for parallel workers, for example 0,2,4",
    )
    parser.add_argument(
        "--crash-point",
        help=(
            "test-only forced termination point, for example "
            "shuffle_left_after:2, shuffle_right_during:100, or join_after:1"
        ),
    )
    parser.add_argument(
        "--output",
        help="benchmark report output path",
    )
    return parser


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json_dumps_stable(value) + "\n")


def _validate_command_options(args: argparse.Namespace) -> None:
    if args.partitions is not None and args.command not in {"run", "resume"}:
        fail("E_BAD_CONFIG", "--partitions is only valid for run/resume")
    if args.crash_point is not None and args.command not in {"run", "resume"}:
        fail("E_BAD_CONFIG", "--crash-point is only valid for run/resume")
    if args.output is not None and args.command != "benchmark":
        fail("E_BAD_CONFIG", "--output is only valid for benchmark")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _validate_command_options(args)
        sample_path = Path(args.sample)
        config = load_sample(sample_path)
        state_path = Path(args.state_dir)
        if args.command == "plan":
            _emit(plan_query(config, args.scale))
            return 0
        if args.command == "run":
            _emit(
                execute_query(
                    config,
                    state_dir=state_path,
                    scale_name=args.scale,
                    partition_text=args.partitions,
                    action="run",
                    crash_point=args.crash_point,
                )
            )
            return 0
        if args.command == "resume":
            if not (state_path / "state.sqlite3").exists():
                fail(
                    "E_NOT_FOUND",
                    "no state database exists to resume",
                    details={"state_dir": str(state_path.resolve())},
                    exit_code=3,
                )
            _emit(
                execute_query(
                    config,
                    state_dir=state_path,
                    scale_name=args.scale,
                    partition_text=args.partitions,
                    action="resume",
                    crash_point=args.crash_point,
                )
            )
            return 0
        if args.command == "status":
            _emit(status_query(config, state_path))
            return 0
        if args.command == "verify":
            if not (state_path / "state.sqlite3").exists():
                fail(
                    "E_NOT_FOUND",
                    "no state database exists to verify",
                    details={"state_dir": str(state_path.resolve())},
                    exit_code=3,
                )
            _emit(
                verify_query(
                    config,
                    state_dir=state_path,
                    scale_name=args.scale,
                )
            )
            return 0
        if args.command == "benchmark":
            _emit(
                benchmark_query(
                    config,
                    state_dir=state_path,
                    output_path=args.output,
                )
            )
            return 0
        fail("E_INTERNAL", f"unhandled command: {args.command}", exit_code=1)
    except EngineError as exc:
        sys.stderr.write(json_dumps_stable(exc.to_dict()) + "\n")
        return exc.exit_code
    except Exception as exc:
        sys.stderr.write(
            json_dumps_stable(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                        "type": type(exc).__name__,
                    },
                }
            )
            + "\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

