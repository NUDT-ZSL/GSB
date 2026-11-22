from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import Any

from .engine import Engine
from .errors import E_USAGE, EngineError
from .util import pretty_json


def _add_common_options(
    target: argparse.ArgumentParser, *, suppress_defaults: bool
) -> None:
    target.add_argument(
        "--samples",
        default="samples" if not suppress_defaults else argparse.SUPPRESS,
        help="sample bundle directory (default: samples)",
    )
    target.add_argument(
        "--db",
        default=(
            "state/mcde.sqlite3"
            if not suppress_defaults
            else argparse.SUPPRESS
        ),
        help="SQLite state path (default: state/mcde.sqlite3)",
    )
    target.add_argument(
        "--lock-dir",
        default=None if not suppress_defaults else argparse.SUPPRESS,
        help="file-lock directory (default: next to the SQLite file)",
    )


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common_options(common, suppress_defaults=False)
    sub_common = argparse.ArgumentParser(add_help=False)
    _add_common_options(sub_common, suppress_defaults=True)
    parser = argparse.ArgumentParser(
        prog="mcde",
        description=(
            "Local deterministic multi-cluster configuration "
            "distribution engine"
        ),
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "load",
        parents=[sub_common],
        help="validate and persist the sample bundle",
    )
    subparsers.add_parser(
        "merge",
        parents=[sub_common],
        help="build and audit three-layer merges",
    )

    plan_parser = subparsers.add_parser(
        "plan",
        parents=[sub_common],
        help="preview policy, quota and dispatch scheduling",
    )
    plan_parser.add_argument(
        "--abandon",
        action="append",
        default=[],
        metavar="CLUSTER",
        help="explicitly abandon a cluster in this request",
    )

    apply_parser = subparsers.add_parser(
        "apply",
        parents=[sub_common],
        help="commit a version and execute eligible dispatches",
    )
    apply_parser.add_argument(
        "--abandon",
        action="append",
        default=[],
        metavar="CLUSTER",
        help="explicitly abandon a cluster in this request",
    )
    apply_parser.add_argument(
        "--version",
        type=int,
        default=None,
        help="request an explicit monotonic version number",
    )
    subparsers.add_parser(
        "resume",
        parents=[sub_common],
        help="finish started work and retry resumable dispatches",
    )
    subparsers.add_parser(
        "status",
        parents=[sub_common],
        help="show stable per-cluster state",
    )
    subparsers.add_parser(
        "verify",
        parents=[sub_common],
        help="compare expected configuration with cluster readback",
    )
    return parser


def _emit(value: Any) -> None:
    print(pretty_json(value))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    engine = Engine(
        samples_dir=args.samples,
        db_path=args.db,
        lock_dir=args.lock_dir,
    )
    try:
        if args.command == "load":
            result = engine.load()
        elif args.command == "merge":
            result = engine.merge()
        elif args.command == "plan":
            result = engine.plan(abandoned=args.abandon)
        elif args.command == "apply":
            result = engine.apply(
                abandoned=args.abandon,
                requested_version=args.version,
            )
        elif args.command == "resume":
            result = engine.resume()
        elif args.command == "status":
            result = engine.status()
        elif args.command == "verify":
            result = engine.verify()
        else:
            raise EngineError(
                E_USAGE, f"unknown command: {args.command}"
            )
        _emit(result)
        if args.command == "verify" and not result.get("ok", True):
            return 4
        return 0
    except EngineError as exc:
        _emit(exc.to_dict())
        return exc.exit_code
    except sqlite3.Error as exc:
        error = EngineError(
            "E_SQLITE",
            "SQLite operation failed",
            details={"reason": str(exc)},
            exit_code=3,
        )
        _emit(error.to_dict())
        return error.exit_code
    except OSError as exc:
        error = EngineError(
            "E_IO",
            "local file operation failed",
            details={"reason": str(exc)},
            exit_code=3,
        )
        _emit(error.to_dict())
        return error.exit_code
    except KeyboardInterrupt:
        _emit(
            EngineError(
                "E_INTERRUPTED", "operation interrupted by user"
            ).to_dict()
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())

