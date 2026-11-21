"""Command-line surface with stable, machine-readable JSON output."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Iterable

from .definitions import parse_v1, parse_v2
from .errors import EngineError, fail
from .logs import LogPackage, parse_logs
from .merge import merge_definitions
from .package import load_package
from .storage import (
    initialize,
    query,
    refresh_logs,
    replace_package,
    resume,
    status,
    verify,
)
from .util import atomic_write_text, pretty_json


def _print(value: Any) -> None:
    sys.stdout.write(pretty_json(value) + "\n")


def _add_package_or_logs(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--package", type=Path, help="versioned package.json")
    group.add_argument("--logs", type=Path, help="log manifest or directory")


def _load_logs(
    *,
    db_path: Path | None = None,
    package_path: Path | None = None,
    logs_path: Path | None = None,
) -> LogPackage:
    if package_path is not None:
        package = load_package(package_path)
        if package.logs is None:
            raise fail("E_LOG_EMPTY", "package does not declare a log package")
        return package.logs
    if logs_path is not None:
        return parse_logs(logs_path)
    if db_path is not None:
        # The stored path is intentionally not printed; it is only a resume hint.
        from .storage import _meta_get, read_connection

        with read_connection(db_path) as connection:
            stored = _meta_get(connection, "current_log_path")
        if stored:
            return parse_logs(Path(stored))
    raise fail("E_RESUME_NO_LOG", "no log package was supplied or recorded")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mv-engine",
        description="Local deterministic incremental materialized-view engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="install an initial definition package")
    load_parser.add_argument("--db", type=Path, required=True)
    load_parser.add_argument("--package", type=Path, required=True)
    load_parser.add_argument("--with-logs", action="store_true")
    load_parser.add_argument("--timeout", type=float, default=30.0)

    merge_parser = subparsers.add_parser("merge", help="merge v1 and v2 definitions into an index")
    merge_parser.add_argument("--v1", type=Path, required=True)
    merge_parser.add_argument("--v2", type=Path, required=True)
    merge_parser.add_argument("--out", type=Path)

    refresh_parser = subparsers.add_parser("refresh", help="apply a validated log package")
    refresh_parser.add_argument("--db", type=Path, required=True)
    _add_package_or_logs(refresh_parser)
    refresh_parser.add_argument("--until-seq", type=int)
    refresh_parser.add_argument("--kill-after", type=int, help=argparse.SUPPRESS)
    refresh_parser.add_argument("--timeout", type=float, default=30.0)

    query_parser = subparsers.add_parser("query", help="query committed view results")
    query_parser.add_argument("--db", type=Path, required=True)
    query_parser.add_argument("--view")
    query_parser.add_argument("--clock-ms", type=int)

    replace_parser = subparsers.add_parser("replace", help="classify and install a new package")
    replace_parser.add_argument("--db", type=Path, required=True)
    replace_parser.add_argument("--package", type=Path, required=True)
    replace_parser.add_argument("--with-logs", action="store_true")
    replace_parser.add_argument(
        "--failpoint",
        choices=["replace_after_stage"],
        help=argparse.SUPPRESS,
    )
    replace_parser.add_argument("--timeout", type=float, default=30.0)

    resume_parser = subparsers.add_parser("resume", help="resume an interrupted refresh")
    resume_parser.add_argument("--db", type=Path, required=True)
    _add_package_or_logs(resume_parser)
    resume_parser.add_argument("--timeout", type=float, default=30.0)

    status_parser = subparsers.add_parser("status", help="show stable engine status JSON")
    status_parser.add_argument("--db", type=Path, required=True)

    verify_parser = subparsers.add_parser("verify", help="deep-replay logs and compare results")
    verify_parser.add_argument("--db", type=Path, required=True)
    _add_package_or_logs(verify_parser)
    verify_parser.add_argument("--snapshots", type=Path)

    return parser


def _run(args: argparse.Namespace) -> int:
    if args.command == "merge":
        v1 = parse_v1(args.v1)
        v2 = parse_v2(args.v2)
        index = merge_definitions(v1, v2)
        text = pretty_json(index.payload) + "\n"
        if args.out is not None:
            atomic_write_text(args.out, text)
        sys.stdout.write(text)
        return 0

    if args.command == "load":
        package = load_package(args.package)
        result = initialize(package, args.db, timeout=args.timeout)
        if args.with_logs:
            if package.logs is None:
                raise fail("E_LOG_EMPTY", "package does not declare a log package")
            result["refresh"] = refresh_logs(
                db_path=args.db,
                logs=package.logs,
                timeout=args.timeout,
            )
        _print(result)
        return 0

    if args.command == "refresh":
        logs = _load_logs(
            db_path=args.db,
            package_path=args.package,
            logs_path=args.logs,
        )
        result = refresh_logs(
            db_path=args.db,
            logs=logs,
            until_seq=args.until_seq,
            kill_after=args.kill_after,
            timeout=args.timeout,
        )
        _print(result)
        return 0

    if args.command == "query":
        if args.clock_ms is not None and args.clock_ms < 0:
            raise fail("E_SCHEMA_INVALID", "clock-ms must be non-negative")
        _print(query(db_path=args.db, view_name=args.view, clock_ms=args.clock_ms))
        return 0

    if args.command == "replace":
        package = load_package(args.package)
        result = replace_package(
            db_path=args.db,
            package=package,
            timeout=args.timeout,
            failpoint=args.failpoint,
        )
        if args.with_logs and package.logs is not None:
            result["refresh"] = refresh_logs(
                db_path=args.db,
                logs=package.logs,
                timeout=args.timeout,
            )
        _print(result)
        return 0

    if args.command == "resume":
        logs = _load_logs(
            db_path=args.db,
            package_path=args.package,
            logs_path=args.logs,
        )
        _print(resume(db_path=args.db, logs=logs, timeout=args.timeout))
        return 0

    if args.command == "status":
        _print(status(args.db))
        return 0

    if args.command == "verify":
        snapshots = args.snapshots
        if snapshots is None and args.package is not None:
            snapshots = load_package(args.package).snapshots_path
        logs = _load_logs(
            db_path=args.db,
            package_path=args.package,
            logs_path=args.logs,
        )
        result = verify(db_path=args.db, logs=logs, snapshots_dir=snapshots)
        _print(result)
        return 0 if result["ok"] else 1

    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return _run(args)
    except EngineError as exc:
        sys.stderr.write(pretty_json(exc.as_dict()) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

