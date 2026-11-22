from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import Config
from .engine import Engine
from .errors import ArchiveError, InjectedCrash


def _json_output(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _error_output(error: ArchiveError) -> None:
    print(
        json.dumps(
            error.as_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="retention",
        description=(
            "Deterministic transaction-log retention policy merger and archival "
            "adjudicator."
        ),
    )
    parser.add_argument(
        "--config",
        default="sample/config.json",
        help="path to the deterministic JSON config (default: sample/config.json)",
    )
    parser.add_argument("--db", help="override the SQLite ledger path")
    parser.add_argument("--clock-ms", type=int, help="override injected logical clock")
    parser.add_argument(
        "--target-ms", type=int, help="override archive target watermark"
    )
    parser.add_argument("--load-version", type=int, help="override input load version")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "load", help="validate and atomically load streams and policies"
    )
    subparsers.add_parser(
        "merge", help="merge v1/v2 policies and build the segment view"
    )

    judge = subparsers.add_parser(
        "judge", help="compute fixed-point margins and adjudicate every log segment"
    )
    judge.add_argument(
        "--instance",
        action="append",
        dest="instances",
        help="stage only this replay instance (repeatable, for parallel workers)",
    )
    judge.add_argument(
        "--stage-only",
        action="store_true",
        help="stage calculations but do not commit a decision view",
    )

    subparsers.add_parser(
        "rebuild",
        help="tombstone conflict losers and rebuild from the nearest common ancestor",
    )
    subparsers.add_parser(
        "resume", help="recover staged or half-finalized judge/rebuild operations"
    )
    subparsers.add_parser(
        "status", help="show the committed view pointer and effective policies"
    )
    subparsers.add_parser(
        "verify",
        help="recompute the current view and compare it with the durable ledger",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> Config:
    base = Config.load(Path(args.config))
    return base.with_overrides(
        ledger_db=args.db,
        clock_ms=args.clock_ms,
        archive_target_ms=args.target_ms,
        load_version=args.load_version,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = _config_from_args(args)
        engine = Engine(config)
        try:
            if args.command == "load":
                result = engine.load()
            elif args.command == "merge":
                result = engine.merge()
            elif args.command == "judge":
                result = engine.judge(
                    requested_clock_ms=config.injected_clock_ms,
                    instances=args.instances,
                    finalize=not args.stage_only,
                )
            elif args.command == "rebuild":
                result = engine.rebuild(requested_clock_ms=config.injected_clock_ms)
            elif args.command == "resume":
                result = engine.resume()
            elif args.command == "status":
                result = engine.status()
            elif args.command == "verify":
                result = engine.verify()
            else:
                raise ArchiveError(
                    "E_COMMAND", "unsupported command", command=args.command
                )
        finally:
            engine.close()
    except ArchiveError as error:
        _error_output(error)
        return 2
    except InjectedCrash as error:
        print(
            json.dumps(
                {
                    "fault_injected": True,
                    "message": str(error),
                    "persisted_state": "recover with the resume command",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 99
    _json_output(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
