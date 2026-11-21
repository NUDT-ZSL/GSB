from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .engine import Engine
from .errors import WalRecError


def _policy_overrides(args: argparse.Namespace) -> dict[str, Any]:
    values = {
        "max_concurrent_txs": args.max_concurrent_txs,
        "round_budget_ms": args.round_budget_ms,
        "checkpoint_interval_ms": args.checkpoint_interval_ms,
        "clock_step_ms": args.clock_step_ms,
        "clock_tolerance_ms": args.clock_tolerance_ms,
        "inject_failure_tx_ids": tuple(args.failure_txid or ()),
    }
    return {key: value for key, value in values.items() if value is not None}


def _add_common(
    parser: argparse.ArgumentParser,
    *,
    sample_required: bool,
    sample_option: bool = True,
) -> None:
    parser.add_argument("--db", required=True, help="SQLite state database path")
    if sample_option:
        parser.add_argument(
            "--sample",
            required=sample_required,
            help="deterministic sample directory",
        )
    parser.add_argument("--json-out", help="also write stable JSON to this path")


def _add_schedule_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--clock-ms", type=int, help="injected logical clock (ms)")
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument("--no-auto-checkpoint", action="store_true")
    parser.add_argument("--max-concurrent-txs", type=int)
    parser.add_argument("--round-budget-ms", type=int)
    parser.add_argument("--checkpoint-interval-ms", type=int)
    parser.add_argument("--clock-step-ms", type=int)
    parser.add_argument("--clock-tolerance-ms", type=int)
    parser.add_argument(
        "--failure-txid",
        type=int,
        action="append",
        help="inject a post-start failure for this transaction id",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="walrec",
        description="Deterministic WAL replay and checkpoint recovery",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate sample and seed state")
    _add_common(load, sample_required=True)
    load.add_argument("--checkpoint-gen", type=int)
    load.add_argument("--clock-ms", type=int)
    load.add_argument("--reset", action="store_true")

    apply_parser = subparsers.add_parser("apply", help="replay pending WAL")
    _add_common(apply_parser, sample_required=True)
    _add_schedule_options(apply_parser)

    checkpoint = subparsers.add_parser(
        "checkpoint",
        help="create or install a v2 checkpoint",
    )
    _add_common(checkpoint, sample_required=False)
    checkpoint.add_argument("--clock-ms", type=int)
    checkpoint.add_argument("--package", help="install an existing v2 package")
    checkpoint.add_argument("--output-dir", help="checkpoint package output root")

    resume = subparsers.add_parser(
        "resume",
        help="recover persisted state and replay unfinished transactions",
    )
    _add_common(resume, sample_required=True)
    _add_schedule_options(resume)

    status = subparsers.add_parser("status", help="print stable state status")
    _add_common(status, sample_required=False)

    verify = subparsers.add_parser("verify", help="verify persisted state")
    _add_common(verify, sample_required=False)
    return parser


def _execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    engine = Engine(args.db)
    command = args.command
    if command == "load":
        result = engine.load(
            args.sample,
            checkpoint_gen=args.checkpoint_gen,
            reset=args.reset,
            clock_start_ms=args.clock_ms,
        )
        return result, 0
    if command == "apply":
        result = engine.apply(
            args.sample,
            clock_ms=args.clock_ms,
            max_rounds=args.max_rounds,
            no_auto_checkpoint=args.no_auto_checkpoint,
            policy_overrides=_policy_overrides(args),
        )
        return result, 0 if result.get("ok") else 3
    if command == "checkpoint":
        result = engine.checkpoint(
            args.sample,
            clock_ms=args.clock_ms,
            package_path=args.package,
            output_dir=args.output_dir,
        )
        return result, 0
    if command == "resume":
        result = engine.resume(
            args.sample,
            clock_ms=args.clock_ms,
            max_rounds=args.max_rounds,
            no_auto_checkpoint=args.no_auto_checkpoint,
            policy_overrides=_policy_overrides(args),
        )
        return result, 0 if result.get("ok") else 3
    if command == "status":
        return engine.status(args.sample), 0
    if command == "verify":
        result = engine.verify(args.sample)
        return result, 0 if result["ok"] else 1
    raise AssertionError(f"unknown command: {command}")


def _emit(value: dict[str, Any], json_out: str | None) -> None:
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )
    print(text)
    if json_out:
        path = Path(json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = _execute(args)
    except WalRecError as exc:
        _emit(exc.to_dict(), getattr(args, "json_out", None))
        return 2
    except (OSError, ValueError, sqlite3.Error) as exc:
        error = WalRecError(
            "E_INTERNAL",
            str(exc),
            details={"exception": type(exc).__name__},
        )
        _emit(error.to_dict(), getattr(args, "json_out", None))
        return 2
    _emit(result, getattr(args, "json_out", None))
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
