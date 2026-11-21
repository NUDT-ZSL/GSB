from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .errors import BusinessError
from .service import (
    apply_patches,
    decide,
    dispatch,
    load_database,
    resume,
    status,
    verify,
)


def _add_db_argument(parser: argparse.ArgumentParser, *, command: bool) -> None:
    parser.add_argument(
        "--db",
        dest="command_db" if command else "db",
        default=None if command else "var/control.db",
        help="SQLite database path (default: var/control.db)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="elasticctl",
        description="Deterministic local metric-driven elastic scaling control plane",
    )
    parser.add_argument("--version", action="version", version=__version__)
    _add_db_argument(parser, command=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load local samples")
    _add_db_argument(load_parser, command=True)
    load_parser.add_argument("--workloads", default="samples/workloads.json")
    load_parser.add_argument("--metrics", default="samples/metric_sources.json")
    load_parser.add_argument("--rules", default="samples/scaling_rules.json")
    load_parser.add_argument("--budget", default="samples/resource_budget.json")
    load_parser.add_argument("--samples", default="samples/metric_stream.json")
    load_parser.add_argument("--outcomes", default="samples/action_outcomes.json")
    load_parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an already initialized database",
    )

    apply_parser = subparsers.add_parser("apply", help="apply an incremental patch")
    _add_db_argument(apply_parser, command=True)
    apply_parser.add_argument("--patch", default="samples/rule_patches.json")

    decide_parser = subparsers.add_parser("decide", help="compute a decision round")
    _add_db_argument(decide_parser, command=True)
    decide_parser.add_argument("--at", type=int, default=None)
    decide_parser.add_argument(
        "--workload",
        action="append",
        dest="workloads",
        default=None,
        help="limit the round to a workload; repeat for multiple workloads",
    )

    dispatch_parser = subparsers.add_parser(
        "dispatch",
        help="decide, reserve budget, and dispatch actions",
    )
    _add_db_argument(dispatch_parser, command=True)
    dispatch_parser.add_argument("--at", type=int, default=None)
    dispatch_parser.add_argument(
        "--workload",
        action="append",
        dest="workloads",
        default=None,
    )
    dispatch_parser.add_argument("--workers", type=int, default=4)

    resume_parser = subparsers.add_parser(
        "resume",
        help="retry only unconfirmed persisted actions",
    )
    _add_db_argument(resume_parser, command=True)
    resume_parser.add_argument("--workers", type=int, default=4)

    status_parser = subparsers.add_parser(
        "status",
        help="print the current stable JSON decision snapshot",
    )
    _add_db_argument(status_parser, command=True)

    verify_parser = subparsers.add_parser(
        "verify",
        help="verify snapshot and accounting invariants",
    )
    _add_db_argument(verify_parser, command=True)
    return parser


def _print_json(payload: Any) -> None:
    sys.stdout.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def _selected_db(args: argparse.Namespace) -> str:
    return str(getattr(args, "command_db", None) or args.db)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_file = _selected_db(args)
    try:
        if args.command == "load":
            payload = load_database(
                db_file,
                workloads_path=Path(args.workloads),
                metrics_path=Path(args.metrics),
                rules_path=Path(args.rules),
                budget_path=Path(args.budget),
                samples_path=Path(args.samples),
                outcomes_path=(
                    None if args.outcomes in (None, "-") else Path(args.outcomes)
                ),
                replace=args.replace,
            )
        elif args.command == "apply":
            payload = apply_patches(db_file, Path(args.patch))
        elif args.command == "decide":
            payload = decide(
                db_file,
                at=args.at,
                workload_ids=args.workloads,
            )
        elif args.command == "dispatch":
            payload = dispatch(
                db_file,
                at=args.at,
                workload_ids=args.workloads,
                workers=args.workers,
            )
        elif args.command == "resume":
            payload = resume(db_file, workers=args.workers)
        elif args.command == "status":
            payload = status(db_file)
        elif args.command == "verify":
            payload = verify(db_file)
        else:
            parser.error(f"unsupported command {args.command}")
            return 2
    except BusinessError as exc:
        sys.stderr.write(
            json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        )
        return 2
    except Exception as exc:
        error = {
            "ok": False,
            "error": {
                "code": "E_INTERNAL",
                "message": str(exc),
                "type": type(exc).__name__,
            },
        }
        sys.stderr.write(
            json.dumps(error, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )
        return 1
    _print_json(payload)
    if args.command == "verify" and not payload.get("ok", False):
        return 1
    return 0

