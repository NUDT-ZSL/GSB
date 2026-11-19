"""Command-line interface for the local block snapshot store."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .engine import BlockSnapshotStore
from .errors import BusinessError
from .model import load_sample


def _json_output(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blockstore",
        description="Deterministic local block snapshots with rollback and recovery",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="initialize a store from a sample")
    load_parser.add_argument("--state", required=True, help="state directory")
    load_parser.add_argument("--sample", required=True, help="sample JSON path")

    snap_parser = subparsers.add_parser("snapshot", help="commit a planned snapshot")
    snap_parser.add_argument("--state", required=True, help="state directory")
    snap_parser.add_argument(
        "--sequence", required=True, type=int, help="planned snapshot sequence"
    )

    rollback_parser = subparsers.add_parser("rollback", help="apply a planned rollback")
    rollback_parser.add_argument("--state", required=True, help="state directory")
    rollback_parser.add_argument(
        "--request-id", required=True, help="planned rollback request id"
    )

    gc_parser = subparsers.add_parser("gc", help="reclaim zero-reference blocks")
    gc_parser.add_argument("--state", required=True, help="state directory")

    resume_parser = subparsers.add_parser(
        "resume", help="recover pending operations and unfinished GC"
    )
    resume_parser.add_argument("--state", required=True, help="state directory")

    status_parser = subparsers.add_parser("status", help="print stable chain status")
    status_parser.add_argument("--state", required=True, help="state directory")

    verify_parser = subparsers.add_parser(
        "verify", help="verify metadata, block files, and visible volume"
    )
    verify_parser.add_argument("--state", required=True, help="state directory")
    return parser


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    store = BlockSnapshotStore(args.state)
    if args.command == "load":
        sample = load_sample(args.sample)
        return store.load(sample), 0
    if args.command == "snapshot":
        return store.snapshot(args.sequence), 0
    if args.command == "rollback":
        return store.rollback(args.request_id), 0
    if args.command == "gc":
        return store.gc(), 0
    if args.command == "resume":
        return store.resume(), 0
    if args.command == "status":
        return store.status(), 0
    if args.command == "verify":
        result = store.verify()
        return result, 0 if result.get("ok") else 2
    raise BusinessError("UNKNOWN_COMMAND", "unknown command", details={"command": args.command})


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = run(args)
    except BusinessError as exc:
        _json_output(exc.as_dict())
        return 1
    except Exception as exc:  # Defensive CLI boundary with a stable JSON shape.
        error = BusinessError(
            "INTERNAL_ERROR",
            "unexpected internal failure",
            details={"exception": type(exc).__name__, "message": str(exc)},
        )
        _json_output(error.as_dict())
        return 1
    _json_output(result)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
