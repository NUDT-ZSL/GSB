from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .engine import Engine
from .errors import FlowAggError, fail
from .plan import load_plan


def stable_json(value: Any) -> str:
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    )


def emit(value: Any, output: str | None = None) -> None:
    text = stable_json(value)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".flowagg-out-", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
    sys.stdout.write(text)


def _base_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowagg",
        description="Deterministic, budgeted loopback traffic aggregation engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--plan", required=True, help="aggregation plan JSON")
        sub.add_argument("--db", required=True, help="SQLite state file")
        sub.add_argument("--lock-timeout", type=float, default=30.0)
        sub.add_argument("--output", help="also write stable JSON to this path")

    load = subparsers.add_parser("load", help="initialize or load a batch")
    common(load)
    load.add_argument("--input", help="optional JSONL batch to aggregate")
    load.add_argument("--batch-id")

    aggregate = subparsers.add_parser("aggregate", help="aggregate a batch or manifest")
    common(aggregate)
    aggregate.add_argument("--input", help="JSONL batch file")
    aggregate.add_argument("--batch-id")
    aggregate.add_argument("--manifest", help="batch manifest JSON")

    resume = subparsers.add_parser("resume", help="recover interrupted processing batches")
    common(resume)
    resume.add_argument("--manifest", help="optionally continue all batches in a manifest")

    status = subparsers.add_parser("status", help="emit stable state summary")
    common(status)
    status.add_argument("--with-groups", action="store_true")

    verify = subparsers.add_parser("verify", help="verify state, spill chains, and truth")
    common(verify)
    verify.add_argument("--manifest", required=True)
    return parser


def _engine(args: argparse.Namespace) -> Engine:
    plan = load_plan(args.plan)
    return Engine(plan, args.db, lock_timeout_seconds=args.lock_timeout)


def run(args: argparse.Namespace) -> dict[str, Any]:
    engine = _engine(args)
    if args.command == "load":
        if bool(args.input) != bool(args.batch_id):
            raise fail(
                "E_ARGUMENT",
                "load requires both --input and --batch-id, or neither",
            )
        if args.input:
            result = engine.aggregate_path(args.input, args.batch_id)
            result["command"] = "load"
            return result
        return engine.load()
    if args.command == "aggregate":
        if args.manifest:
            if args.input or args.batch_id:
                raise fail(
                    "E_ARGUMENT",
                    "use either --manifest or --input/--batch-id, not both",
                )
            return engine.aggregate_manifest(args.manifest)
        if not args.input or not args.batch_id:
            raise fail(
                "E_ARGUMENT",
                "aggregate requires --input and --batch-id, or --manifest",
            )
        return engine.aggregate_path(args.input, args.batch_id)
    if args.command == "resume":
        return engine.resume(args.manifest)
    if args.command == "status":
        return engine.status(with_groups=args.with_groups)
    if args.command == "verify":
        return engine.verify(args.manifest)
    raise fail("E_ARGUMENT", f"unknown command {args.command!r}")


def main(argv: list[str] | None = None) -> int:
    parser = _base_parser()
    try:
        args = parser.parse_args(argv)
        result = run(args)
        emit(result, args.output)
        return 0
    except FlowAggError as exc:
        sys.stderr.write(stable_json(exc.as_json()))
        return exc.exit_code
    except KeyboardInterrupt:
        exc = fail("E_INTERNAL", "interrupted by user")
        sys.stderr.write(stable_json(exc.as_json()))
        return exc.exit_code
    except Exception as exc:
        wrapped = fail(
            "E_INTERNAL",
            f"{type(exc).__name__}: {exc}",
        )
        sys.stderr.write(stable_json(wrapped.as_json()))
        return wrapped.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

