from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_json
from .engine import Workbench
from .errors import E_INTERNAL, WorkbenchError


def _emit(value: Any, output: Path | None = None) -> None:
    text = canonical_json(value) + "\n"
    if output is None:
        sys.stdout.write(text)
        sys.stdout.flush()
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8", newline="\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obs-workbench",
        description="Deterministic event-time window denoising and topology invalidation workbench.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "workbench.sqlite3",
        help="SQLite database path (default: data/workbench.sqlite3)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate and load a topology snapshot")
    load.add_argument("--topology", type=Path, required=True)

    ingest = subparsers.add_parser("ingest", help="ingest an event bucket file")
    ingest.add_argument("--events", type=Path, required=True)
    ingest.add_argument(
        "--isolate-buckets",
        action="store_true",
        help="commit valid buckets while reporting per-bucket validation errors",
    )

    apply = subparsers.add_parser("apply", help="apply a topology change patch")
    apply.add_argument("--patch", type=Path, required=True)

    subparsers.add_parser("resume", help="finish staged crash-interrupted propagation")

    status = subparsers.add_parser("status", help="emit a stable JSON status snapshot")
    status.add_argument("--output", type=Path)

    verify = subparsers.add_parser("verify", help="verify persisted records by recomputation")
    verify.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    workbench = Workbench(args.db)
    try:
        if args.command == "load":
            result = workbench.load(args.topology)
        elif args.command == "ingest":
            result = workbench.ingest(args.events, isolate_buckets=args.isolate_buckets)
        elif args.command == "apply":
            result = workbench.apply(args.patch)
        elif args.command == "resume":
            result = workbench.resume()
        elif args.command == "status":
            result = workbench.status()
            _emit(result, args.output)
            return 0
        elif args.command == "verify":
            result = workbench.verify()
            _emit(result, args.output)
            return 0 if result.get("ok") else 2
        else:
            raise AssertionError(f"unhandled command: {args.command}")
        _emit(result)
        if args.command == "ingest" and args.isolate_buckets and not result.get("command_ok", True):
            return 2
        return 0
    except WorkbenchError as exc:
        _emit({"ok": False, "error": exc.to_dict()})
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _emit(
            {
                "ok": False,
                "error": {
                    "code": E_INTERNAL,
                    "details": {"type": type(exc).__name__},
                    "message": str(exc),
                },
            }
        )
        return 1
