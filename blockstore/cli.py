from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any

from .canonical import read_json_file
from .engine import SnapshotEngine
from .errors import BlockStoreError, ErrorCode
from .models import RollbackRequest, load_bundle


def _emit(payload: dict[str, Any], *, compact: bool) -> None:
    if compact:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    print(text)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m blockstore",
        description="Local content-addressed block snapshots and rollback",
    )
    parser.add_argument(
        "--root",
        default="data",
        help="state directory containing SQLite and block files (default: data)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON instead of indented JSON",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_local_options(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--root",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        subparser.add_argument(
            "--compact",
            action="store_true",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )

    load_parser = subparsers.add_parser("load", help="validate and load a plan")
    add_local_options(load_parser)
    load_parser.add_argument("--volume", required=True)
    load_parser.add_argument("--snapshots")
    load_parser.add_argument("--rollbacks")
    load_parser.add_argument(
        "--base-only",
        action="store_true",
        help="initialize only the base volume; do not apply snapshot plan",
    )

    snapshot_parser = subparsers.add_parser(
        "snapshot", help="create one incrementally planned snapshot"
    )
    add_local_options(snapshot_parser)
    snapshot_parser.add_argument(
        "--file",
        required=True,
        help="JSON snapshot object or snapshot plan file",
    )
    snapshot_parser.add_argument(
        "--snapshot-id",
        help="snapshot to select when the file contains multiple snapshots",
    )

    rollback_parser = subparsers.add_parser(
        "rollback", help="roll back to a live snapshot"
    )
    add_local_options(rollback_parser)
    rollback_parser.add_argument(
        "--file",
        help="JSON rollback request object or request list",
    )
    rollback_parser.add_argument("--request-id")
    rollback_parser.add_argument("--target", help="target snapshot id")
    rollback_parser.add_argument(
        "--clock",
        type=int,
        help="rollback logical clock in milliseconds",
    )
    rollback_parser.add_argument(
        "--keep-after-target",
        action="store_true",
        help="retain orphan metadata after rollback (block references always expire)",
    )

    gc_parser = subparsers.add_parser("gc", help="delete zero-reference block files")
    add_local_options(gc_parser)
    resume_parser = subparsers.add_parser("resume", help="resume prepared operations")
    add_local_options(resume_parser)
    status_parser = subparsers.add_parser(
        "status", help="print stable chain status JSON"
    )
    add_local_options(status_parser)
    verify_parser = subparsers.add_parser(
        "verify", help="verify chain, ownership, and refcounts"
    )
    add_local_options(verify_parser)
    return parser


def _run(args: argparse.Namespace) -> dict[str, Any]:
    engine = SnapshotEngine(args.root)
    if args.command == "load":
        bundle = load_bundle(
            args.volume,
            args.snapshots,
            args.rollbacks,
        )
        return engine.load_plan(bundle, apply_plan=not args.base_only)
    if args.command == "snapshot":
        raw = read_json_file(args.file)
        spec = engine.parse_snapshot_file(
            raw,
            snapshot_id=args.snapshot_id,
            context=str(args.file),
        )
        return engine.create_snapshot(spec)
    if args.command == "rollback":
        if args.file:
            raw = read_json_file(args.file)
            request = engine.parse_rollback_file(
                raw,
                request_id=args.request_id,
                context=str(args.file),
            )
        else:
            missing = [
                name
                for name, value in (
                    ("--request-id", args.request_id or None),
                    ("--target", args.target),
                    ("--clock", args.clock),
                )
                if value is None
            ]
            if missing:
                raise BlockStoreError(
                    ErrorCode.INVALID_INPUT,
                    "direct rollback requires --request-id, --target, and --clock",
                    {"missing": missing},
                )
            request = RollbackRequest(
                request_id=args.request_id,
                target_snapshot_id=args.target,
                rollback_clock=args.clock,
                keep_after_target=args.keep_after_target,
            )
        return engine.rollback(request)
    if args.command == "gc":
        return engine.gc()
    if args.command == "resume":
        return engine.resume()
    if args.command == "status":
        return engine.status()
    if args.command == "verify":
        return engine.verify()
    raise BlockStoreError(
        ErrorCode.INVALID_INPUT,
        "unknown command",
        {"command": args.command},
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = _run(args)
    except BlockStoreError as exc:
        _emit({"ok": False, "error": exc.as_dict()}, compact=args.compact)
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        details: dict[str, Any] = {
            "exception": type(exc).__name__,
            "message": str(exc),
        }
        if __import__("os").environ.get("BLOCKSTORE_DEBUG"):
            details["traceback"] = traceback.format_exc()
        _emit(
            {
                "ok": False,
                "error": {
                    "code": ErrorCode.UNEXPECTED.value,
                    "message": "unexpected failure",
                    "details": details,
                },
            },
            compact=args.compact,
        )
        return 2
    _emit(result, compact=args.compact)
    return 0


if __name__ == "__main__":
    sys.exit(main())
