from __future__ import annotations

import argparse
import dataclasses
import sys
from typing import Any

from .canonical import digest_json, dump_stable
from .db import FileLock, open_existing, read_transaction
from .diagnostics import build_status, diagnose, verify
from .errors import WorkbenchError
from .patches import ParsedPatch, apply_patch, parse_patch_file, resume
from .snapshots import load_tracking
from .windows import run_refresh


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="expworkbench",
        description="Deterministic local experiment tracking and diagnostics workbench",
    )
    parser.add_argument("--db", default="workbench.db", help="SQLite workbench path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--db", default=argparse.SUPPRESS, help="SQLite workbench path"
        )

    load = subparsers.add_parser("load", help="Validate and load binary snapshot generations")
    add_common(load)
    load.add_argument("--package", help="snapshot package JSON")
    load.add_argument("--v1", help="v1 single-file snapshot JSON")
    load.add_argument("--v2-manifest", help="v2 shard manifest JSON")
    load.add_argument("--experiments", help="experiment lineage JSON")
    load.add_argument("--metrics", help="metric trajectory JSON")
    load.add_argument("--snapshot-version", help="explicit merged snapshot version")
    load.add_argument("--now-ms", type=_non_negative_int, help="injected logical clock")

    refresh_parser = subparsers.add_parser("refresh", help="Advance metric windows")
    add_common(refresh_parser)
    refresh_parser.add_argument("--now-ms", type=_non_negative_int, required=True)
    refresh_parser.add_argument("--window-ms", type=_positive_int, default=1000)

    patch_parser = subparsers.add_parser("patch", help="Apply a lineage/config patch")
    add_common(patch_parser)
    patch_parser.add_argument("--file", required=True, help="patch JSON")
    patch_parser.add_argument("--now-ms", type=_non_negative_int)
    patch_parser.add_argument("--window-ms", type=_positive_int, default=1000)

    resume_parser = subparsers.add_parser("resume", help="Replay pending invalidation work")
    add_common(resume_parser)

    diagnose_parser = subparsers.add_parser(
        "diagnose", help="Compare incremental state with the full truth recomputation"
    )
    add_common(diagnose_parser)
    diagnose_parser.add_argument("--now-ms", type=_non_negative_int)
    diagnose_parser.add_argument("--window-ms", type=_positive_int)

    status_parser = subparsers.add_parser("status", help="Stable status JSON")
    add_common(status_parser)

    verify_parser = subparsers.add_parser("verify", help="Fail if persisted state diverges")
    add_common(verify_parser)
    verify_parser.add_argument("--now-ms", type=_non_negative_int)
    verify_parser.add_argument("--window-ms", type=_positive_int)

    web_parser = subparsers.add_parser("web", help="Start the local visualization workbench")
    add_common(web_parser)
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=_positive_int, default=5000)
    return parser


def _override_patch_clock(
    path: str, now_ms: int | None, window_ms: int | None
) -> ParsedPatch:
    patch = parse_patch_file(path, default_window_ms=window_ms or 1000)
    if now_ms is None and (window_ms is None or window_ms == patch.window_ms):
        return patch
    updated_now = patch.now_ms if now_ms is None else now_ms
    updated_window = patch.window_ms if window_ms is None else window_ms
    return ParsedPatch(
        patch_id=patch.patch_id,
        now_ms=updated_now,
        window_ms=updated_window,
        operations=patch.operations,
        semantics_digest=digest_json(
            {
                "now_ms": updated_now,
                "window_ms": updated_window,
                "operations": patch.operations,
            }
        ),
        raw=patch.raw,
    )


def execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.command == "load":
        result = load_tracking(
            args.db,
            package_path=args.package,
            v1_path=args.v1,
            v2_manifest_path=args.v2_manifest,
            experiments_path=args.experiments,
            metrics_path=args.metrics,
            explicit_snapshot_version=args.snapshot_version,
            logical_clock_ms=args.now_ms,
        )
        return result, 0
    if args.command == "refresh":
        connection = open_existing(args.db)
        try:
            with FileLock(args.db):
                result = run_refresh(
                    connection, now_ms=args.now_ms, window_ms=args.window_ms
                )
        finally:
            connection.close()
        return result, 0
    if args.command == "patch":
        patch = _override_patch_clock(args.file, args.now_ms, args.window_ms)
        connection = open_existing(args.db)
        try:
            with FileLock(args.db):
                result = apply_patch(connection, patch)
        finally:
            connection.close()
        return result, 0
    if args.command == "resume":
        connection = open_existing(args.db)
        try:
            with FileLock(args.db):
                result = resume(connection)
        finally:
            connection.close()
        return result, 0
    if args.command == "status":
        connection = open_existing(args.db, readonly=True)
        try:
            with read_transaction(connection):
                result = build_status(connection)
        finally:
            connection.close()
        return result, 0
    if args.command == "diagnose":
        connection = open_existing(args.db, readonly=True)
        try:
            with read_transaction(connection):
                result = diagnose(
                    connection, now_ms=args.now_ms, window_ms=args.window_ms
                )
        finally:
            connection.close()
        return result, 0
    if args.command == "verify":
        connection = open_existing(args.db, readonly=True)
        try:
            with read_transaction(connection):
                result = verify(
                    connection, now_ms=args.now_ms, window_ms=args.window_ms
                )
        finally:
            connection.close()
        return result, 0 if result["consistent"] else 1
    if args.command == "web":
        from .web import create_app

        app = create_app(args.db)
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
        return {"ok": True, "operation": "web", "stopped": True}, 0
    raise WorkbenchError(
        code="E_COMMAND",
        message="Unknown command",
        context={"command": args.command},
    )


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print(
            dump_stable(
                {
                    "ok": False,
                    "error": {
                        "code": "E_PYTHON_VERSION",
                        "message": "Python 3.11 or newer is required",
                        "context": {"actual": sys.version.split()[0]},
                    },
                }
            )
        )
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = execute(args)
        print(dump_stable(result))
        return exit_code
    except WorkbenchError as exc:
        print(dump_stable(exc.as_dict()))
        return exc.exit_code
    except KeyboardInterrupt:
        print(
            dump_stable(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERRUPTED",
                        "message": "Operation interrupted",
                        "context": {},
                    },
                }
            )
        )
        return 130

