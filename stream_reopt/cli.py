"""Command-line interface for run, resume, status, and verify."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .engine import CrashPlan, RuntimeEngine
from .errors import BusinessError, ErrorCode
from .models import load_scenario
from .paths import assert_safe_delete
from .verify import verify_against_serial


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sample",
        default="samples/adaptive_join_sample.json",
        help="Path to the deterministic scenario JSON.",
    )
    parser.add_argument(
        "--db",
        default="runtime/state.db",
        help="SQLite state path, relative to the current working directory.",
    )
    parser.add_argument(
        "--blocks",
        default="runtime/blocks",
        help="Local block/shard directory.",
    )
    parser.add_argument(
        "--lock",
        default=None,
        help="Lock file path (defaults to <db>.lock).",
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=30.0,
        help="Cross-process file lock timeout in seconds.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    parser.add_argument(
        "--allow-decision-errors",
        action="store_true",
        help="Return success even when a re-optimization decision reports E_SWITCH_BUDGET.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stream-reopt")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run or continue all pending batches.")
    _add_common(run)
    run.add_argument("--max-batches", type=int, default=None)
    run.add_argument("--fresh", action="store_true", help="Remove existing runtime state before running.")
    run.add_argument("--crash-at-batch", default=None)
    run.add_argument(
        "--crash-point",
        default="after-execute",
        choices=["during-join", "after-generation", "after-execute", "after-staging", "before-commit", "after-commit"],
    )
    resume = sub.add_parser("resume", help="Resume an existing runtime after termination.")
    _add_common(resume)
    resume.add_argument("--max-batches", type=int, default=None)
    resume.add_argument("--crash-at-batch", default=None)
    resume.add_argument(
        "--crash-point",
        default="after-execute",
        choices=["during-join", "after-generation", "after-execute", "after-staging", "before-commit", "after-commit"],
    )
    status = sub.add_parser("status", help="Show persisted watermark, shape, drift, and decision state.")
    _add_common(status)
    verify = sub.add_parser("verify", help="Compare persisted state with a serial single-process truth run.")
    _add_common(verify)
    verify.add_argument("--truth-db", default="runtime/verify_truth/state.db")
    verify.add_argument("--truth-blocks", default="runtime/verify_truth/blocks")
    verify.add_argument("--keep-truth", action="store_true")
    return parser


def _emit(value: dict[str, Any], pretty: bool) -> None:
    json.dump(
        value,
        sys.stdout,
        ensure_ascii=False,
        sort_keys=True,
        indent=2 if pretty else None,
    )
    sys.stdout.write("\n")


def _fresh(paths: Sequence[Path], *, sample: Path) -> None:
    checked = [assert_safe_delete(path, protected=[sample]) for path in paths]
    for path in checked:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pretty = bool(args.pretty)
    try:
        if getattr(args, "max_batches", None) is not None and args.max_batches < 0:
            raise BusinessError(
                ErrorCode.BAD_COMMAND,
                "--max-batches must be non-negative",
                details={"max_batches": args.max_batches},
            )
        if args.lock_timeout <= 0:
            raise BusinessError(
                ErrorCode.BAD_COMMAND,
                "--lock-timeout must be positive",
                details={"lock_timeout": args.lock_timeout},
            )
        scenario = load_scenario(args.sample)
        db_path = Path(args.db)
        blocks_path = Path(args.blocks)
        if args.command in {"run", "resume"}:
            if args.command == "run" and args.fresh:
                lock_path = Path(args.lock) if args.lock else db_path.with_suffix(db_path.suffix + ".lock")
                _fresh(
                    [
                        db_path,
                        Path(str(db_path) + "-wal"),
                        Path(str(db_path) + "-shm"),
                        lock_path,
                        blocks_path,
                    ],
                    sample=Path(args.sample),
                )
            elif args.command == "resume" and not db_path.exists():
                raise BusinessError(
                    ErrorCode.NOT_FOUND,
                    "no persisted runtime state to resume",
                    details={"db_path": str(db_path)},
                )
            engine = RuntimeEngine(
                scenario,
                db_path=db_path,
                blocks_path=blocks_path,
                lock_path=args.lock,
                lock_timeout_s=args.lock_timeout,
            )
            try:
                if args.command == "resume":
                    engine.require_existing()
                crash = (
                    CrashPlan(args.crash_at_batch, args.crash_point)
                    if args.crash_at_batch
                    else None
                )
                result = engine.run(
                    resume=args.command == "resume",
                    max_batches=args.max_batches,
                    crash=crash,
                )
                output = result.to_dict()
                if args.command == "resume":
                    output["command"] = "resume"
            finally:
                engine.close()
            _emit(output, pretty)
            if output.get("decision_error_codes") and not args.allow_decision_errors:
                return 3
            return 0
        if args.command == "status":
            if not db_path.exists():
                raise BusinessError(
                    ErrorCode.NOT_FOUND,
                    "no persisted runtime state to inspect",
                    details={"db_path": str(db_path)},
                )
            engine = RuntimeEngine(
                scenario,
                db_path=db_path,
                blocks_path=blocks_path,
                lock_path=args.lock,
                lock_timeout_s=args.lock_timeout,
            )
            try:
                output = engine.status()
            finally:
                engine.close()
            output["ok"] = True
            output["command"] = "status"
            _emit(output, pretty)
            return 0
        if args.command == "verify":
            result = verify_against_serial(
                scenario,
                db_path=db_path,
                blocks_path=blocks_path,
                truth_db_path=args.truth_db,
                truth_blocks_path=args.truth_blocks,
                reset_truth=not args.keep_truth,
            )
            _emit(result.to_dict(), pretty)
            return 0 if result.consistent else 4
    except BusinessError as exc:
        _emit(exc.to_dict(), pretty)
        return exc.exit_code
    except sqlite3.Error as exc:
        _emit(
            BusinessError(
                ErrorCode.STATE_CORRUPT,
                f"SQLite operation failed: {exc}",
                details={"type": type(exc).__name__},
            ).to_dict(),
            pretty,
        )
        return 2
    except OSError as exc:
        _emit(
            BusinessError(
                ErrorCode.IO,
                f"filesystem operation failed: {exc}",
                details={"type": type(exc).__name__},
            ).to_dict(),
            pretty,
        )
        return 2
    except KeyboardInterrupt:
        _emit(
            BusinessError(ErrorCode.BAD_COMMAND, "interrupted").to_dict(),
            pretty,
        )
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

