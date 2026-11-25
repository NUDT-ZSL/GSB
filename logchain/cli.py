from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from .diagnostics import cmd_load, cmd_verify, cmd_status
from .engine import cmd_merge, cmd_replay
from .errors import ChainError
from .packages import receive_package
from .recovery import cmd_resume
from .util import stable_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logchain",
        description="Deterministic log-chain merge, replay, ledger and recovery diagnostic CLI",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=Path.cwd(), help="workspace root")
    common.add_argument("--clock-ms", type=int, default=None, help="injected logical clock in ms")
    common.add_argument("--crash-at", type=str, default=None, help="crash injection id")

    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", parents=[common], help="validate and load samples/packages")
    load_parser.add_argument("--package", type=Path, default=None, help="snapshot package directory")

    subparsers.add_parser("merge", parents=[common], help="merge v1 and v2 snapshots")
    subparsers.add_parser("replay", parents=[common], help="replay WAL increments")
    subparsers.add_parser("resume", parents=[common], help="recover half writes and replay suffix")
    subparsers.add_parser("status", parents=[common], help="stable JSON diagnostic status")
    subparsers.add_parser("verify", parents=[common], help="verify hashes and full replay equivalence")
    return parser


def run(argv: Sequence[str] | None = None) -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()
    try:
        if args.command == "load":
            if args.package is not None:
                result = receive_package(root, args.package.resolve(), args.clock_ms)
            else:
                result = cmd_load(root, args.clock_ms)
            return 0, stable_json(result)
        if args.command == "merge":
            return 0, stable_json(cmd_merge(root, args.clock_ms, args.crash_at))
        if args.command == "replay":
            return 0, stable_json(cmd_replay(root, args.clock_ms, args.crash_at))
        if args.command == "resume":
            return 0, stable_json(cmd_resume(root, args.clock_ms))
        if args.command == "status":
            return 0, cmd_status(root, args.clock_ms)
        if args.command == "verify":
            result = cmd_verify(root, args.clock_ms)
            return (0 if result.get("ok") else 1), stable_json(result)
        raise ChainError("UNKNOWN_COMMAND", "unsupported command", {"command": args.command})
    except ChainError as exc:
        return exc.exit_code, stable_json(exc.to_dict())
    except (OSError, ValueError, KeyError) as exc:
        error = ChainError(
            "INTERNAL_VALIDATION_ERROR",
            str(exc),
            {"error_type": type(exc).__name__},
        )
        return error.exit_code, stable_json(error.to_dict())


def main(argv: Sequence[str] | None = None) -> int:
    exit_code, output = run(argv)
    if exit_code == 0:
        print(output)
    else:
        print(output, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
