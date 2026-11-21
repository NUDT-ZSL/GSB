"""Command-line interface for the local cold-start engine."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .engine import Engine
from .errors import EngineError
from .util import stable_json


def _state_dir(value: str) -> str:
    return str(Path(value).resolve())


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir",
        default="state",
        help="writable directory for the SQLite ledger and recovery artifacts",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coldstart",
        description="Deterministic local serverless cold-start and concurrency engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="load manifests and merge snapshots")
    _add_common(load)
    load.add_argument("--functions", required=True)
    load.add_argument("--layers", required=True)
    load.add_argument("--snapshot-v1", required=True)
    load.add_argument("--snapshot-v2-index", required=True)
    load.add_argument("--config-version", type=int, default=1)
    load.add_argument("--snapshot-version", type=int, default=1)

    invoke = subparsers.add_parser("invoke", help="process a call trace")
    _add_common(invoke)
    invoke.add_argument("--trace", required=True)
    invoke.add_argument(
        "--failpoint",
        choices=["init-started"],
        default=None,
        help="simulate abrupt process termination after a committed cold-start reservation",
    )

    apply_parser = subparsers.add_parser("apply", help="apply a layer patch")
    _add_common(apply_parser)
    apply_parser.add_argument("--patch", required=True)
    apply_parser.add_argument(
        "--failpoint",
        choices=["mid-apply", "after-commit"],
        default=None,
    )

    resume = subparsers.add_parser("resume", help="replay committed recovery jobs")
    _add_common(resume)

    status = subparsers.add_parser("status", help="print stable instance status")
    _add_common(status)

    verify = subparsers.add_parser("verify", help="verify ledger invariants")
    _add_common(verify)
    return parser


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(stable_json(value))
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        engine = Engine(_state_dir(args.state_dir))
        if args.command == "load":
            result = engine.load(
                args.functions,
                args.layers,
                args.snapshot_v1,
                args.snapshot_v2_index,
                config_version=args.config_version,
                snapshot_version=args.snapshot_version,
            )
        elif args.command == "invoke":
            result = engine.invoke_trace(args.trace, failpoint=args.failpoint)
        elif args.command == "apply":
            result = engine.apply_patch(args.patch, failpoint=args.failpoint)
        elif args.command == "resume":
            result = engine.resume()
        elif args.command == "status":
            result = engine.status()
        elif args.command == "verify":
            result = engine.verify()
        else:
            raise AssertionError(args.command)
        _emit(result)
        return 0 if result.get("ok", False) else 1
    except EngineError as exc:
        _emit(exc.to_dict())
        return 2
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERNAL",
                    "message": str(exc),
                },
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
