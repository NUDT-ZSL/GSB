from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

from .errors import DiagError
from .service import QuotaDiagnostic
from .util import write_json


def _emit(payload: Any, output: str | None = None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if output:
        write_json(output, payload)
    sys.stdout.write(text + "\n")


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="SQLite diagnostic database")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quota-diag",
        description="Deterministic multi-level directory quota diagnostic component.",
    )
    parser.add_argument("--debug", action="store_true", help="show internal tracebacks")
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="load dirs, write stream and both rule generations")
    _add_db(load)
    load.add_argument(
        "--dirs", "--dirs-file", dest="dirs", required=True, help="directory JSON"
    )
    load.add_argument(
        "--writes",
        "--writes-file",
        "--events",
        dest="writes",
        required=True,
        help="write event JSONL",
    )
    load.add_argument(
        "--v1", "--v1-file", dest="v1", required=True, help="v1 rule JSON"
    )
    load.add_argument(
        "--v2",
        "--v2-dir",
        dest="v2",
        required=True,
        help="v2 package directory or meta.db",
    )
    load.add_argument("--output")

    merge = sub.add_parser("merge", help="build the unified rule view")
    _add_db(merge)
    merge.add_argument("--output")

    write = sub.add_parser("write", help="deduct pending write events")
    _add_db(write)
    write.add_argument("--block-size", type=int, default=25)
    write.add_argument("--start-seq", type=int)
    write.add_argument("--end-seq", type=int)
    write.add_argument(
        "--crash-at-seq",
        type=int,
        help="test hook: SIGKILL-equivalent exit inside the event transaction",
    )
    write.add_argument("--output")

    patch = sub.add_parser("patch", help="apply rule/directory invalidation patch")
    _add_db(patch)
    patch.add_argument("--patch", "--patch-file", dest="patch", required=True)
    patch.add_argument("--output")

    snapshot = sub.add_parser(
        "snapshot", help="append or replace a v2 rule package atomically"
    )
    _add_db(snapshot)
    snapshot.add_argument("--v2", "--v2-dir", dest="v2", required=True)
    snapshot.add_argument("--output")

    resume = sub.add_parser("resume", help="replay only uncommitted events")
    _add_db(resume)
    resume.add_argument("--block-size", type=int, default=25)
    resume.add_argument("--output")

    status = sub.add_parser("status", help="show persisted state")
    _add_db(status)
    status.add_argument("--output")

    verify = sub.add_parser("verify", help="compare ledger/audit with full computation")
    _add_db(verify)
    verify.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    diagnostic = QuotaDiagnostic(args.db)
    try:
        if args.command == "load":
            payload = diagnostic.load(
                dirs_path=args.dirs,
                writes_path=args.writes,
                v1_path=args.v1,
                v2_path=args.v2,
            )
        elif args.command == "merge":
            payload = diagnostic.merge()
        elif args.command == "write":
            payload = diagnostic.write(
                block_size=args.block_size,
                start_seq=args.start_seq,
                end_seq=args.end_seq,
                crash_at_seq=args.crash_at_seq,
            )
        elif args.command == "patch":
            payload = diagnostic.patch(patch_path=args.patch)
        elif args.command == "snapshot":
            payload = diagnostic.snapshot(v2_path=args.v2)
        elif args.command == "resume":
            payload = diagnostic.resume(block_size=args.block_size)
        elif args.command == "status":
            payload = diagnostic.status()
        elif args.command == "verify":
            payload = diagnostic.verify()
        else:
            parser.error(f"unsupported command: {args.command}")
            return 2
        _emit(payload, getattr(args, "output", None))
        if args.command == "verify" and not payload["consistent"]:
            return 1
        return 0
    except DiagError as exc:
        _emit(exc.to_dict(), getattr(args, "output", None))
        return 2
    except sqlite3.Error as exc:
        _emit(
            {
                "error": {
                    "code": "E_SQLITE",
                    "message": str(exc),
                    "fields": {"database": args.db},
                }
            },
            getattr(args, "output", None),
        )
        return 2
    except Exception as exc:
        if args.debug:
            raise
        _emit(
            {
                "error": {
                    "code": "E_INTERNAL",
                    "message": str(exc),
                    "fields": {"type": type(exc).__name__},
                }
            },
            getattr(args, "output", None),
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
