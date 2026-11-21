"""Command-line interface with stable JSON output."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_json, pretty_json
from .engine import Engine
from .errors import EngineError


def _add_common(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    default = argparse.SUPPRESS if suppress else None
    if suppress:
        parser.add_argument("--db", default=argparse.SUPPRESS)
        parser.add_argument("--package", default=argparse.SUPPRESS)
    else:
        parser.add_argument("--db", default="state/engine.sqlite")
        parser.add_argument("--package", default="sample/package.json")
    parser.add_argument("--compact", action="store_true", default=default)


def _clock_group(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    group = parser.add_mutually_exclusive_group()
    if suppress:
        group.add_argument("--clock-ms", type=int, default=argparse.SUPPRESS)
        group.add_argument("--clock-step", default=argparse.SUPPRESS)
    else:
        group.add_argument("--clock-ms", type=int)
        group.add_argument("--clock-step")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="config-state",
        description="Deterministic local v1/v2 configuration state evolution engine",
    )
    _add_common(parser, suppress=False)
    _clock_group(parser, suppress=False)
    parser.add_argument(
        "--source",
        choices=("all", "manual", "auto"),
        default="all",
        help="event source for reconcile",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("load", "validate a package without materializing time"),
        ("merge", "validate, migrate, and publish a unified view"),
        ("reconcile", "commit new events and publish the evolved view"),
        ("resume", "finish interrupted operations"),
        ("status", "print committed status and event watermarks"),
        ("verify", "verify persistent state and the visible view"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        _add_common(command, suppress=True)
        if name in {"merge", "reconcile"}:
            _clock_group(command, suppress=True)
        if name == "reconcile":
            command.add_argument(
                "--source",
                choices=("all", "manual", "auto"),
                default=argparse.SUPPRESS,
            )
    return parser


def _render(value: Any, compact: bool) -> str:
    if compact:
        return canonical_json(value) + "\n"
    return pretty_json(value) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db)
    package_path = Path(args.package)
    compact = bool(getattr(args, "compact", False))
    engine = Engine(db_path)
    try:
        if args.command == "load":
            result = engine.load(package_path)
        elif args.command == "merge":
            result = engine.merge(
                package_path,
                clock_ms=getattr(args, "clock_ms", None),
                clock_step=getattr(args, "clock_step", None),
            )
        elif args.command == "reconcile":
            source = getattr(args, "source", "all")
            sources = ("manual", "auto") if source == "all" else (source,)
            result = engine.reconcile(
                package_path,
                clock_ms=getattr(args, "clock_ms", None),
                clock_step=getattr(args, "clock_step", None),
                sources=sources,
            )
        elif args.command == "resume":
            result = engine.resume()
        elif args.command == "status":
            result = engine.status(package_path)
        elif args.command == "verify":
            result = engine.verify()
        else:
            raise EngineError("INVALID_ARGUMENT", f"unknown command: {args.command}")
    except EngineError as exc:
        sys.stdout.write(_render(exc.as_dict(), compact))
        return 2
    except Exception as exc:  # Defensive: keep CLI output machine-readable.
        failure = {
            "code": "INTERNAL_ERROR",
            "details": {"type": type(exc).__name__},
            "message": str(exc),
            "ok": False,
        }
        sys.stdout.write(_render(failure, compact))
        return 3

    sys.stdout.write(_render(result, compact))
    if args.command == "verify" and not result.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

