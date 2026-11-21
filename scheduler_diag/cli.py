from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from .canonical import stable_json
from .errors import BusinessError
from .service import (
    inject_workspace,
    load_workspace,
    resume_workspace,
    replay_workspace,
    schedule_workspace,
    status_workspace,
    verify_workspace,
    write_report,
)


def _add_common(parser: argparse.ArgumentParser, *, bundle_required: bool) -> None:
    parser.add_argument(
        "--db",
        default="var/workbench.sqlite",
        help="SQLite state path (default: var/workbench.sqlite)",
    )
    parser.add_argument(
        "--bundle",
        required=bundle_required,
        help="bundle directory containing the four local JSON files",
    )
    parser.add_argument("--out", help="also write stable JSON to this report path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scheduler-diag",
        description="Deterministic local scheduling and attribution workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load a bundle")
    _add_common(load_parser, bundle_required=True)
    load_parser.add_argument(
        "--reset",
        action="store_true",
        help="delete existing state and initialize a new ledger epoch",
    )

    schedule_parser = subparsers.add_parser(
        "schedule", help="schedule all pending Pods"
    )
    _add_common(schedule_parser, bundle_required=True)
    schedule_parser.add_argument(
        "--crash-at",
        choices=("filter", "score", "bind"),
        help="diagnostic forced termination point before transaction commit",
    )

    resume_parser = subparsers.add_parser(
        "resume", help="recover state and schedule all remaining Pods"
    )
    _add_common(resume_parser, bundle_required=True)
    resume_parser.add_argument(
        "--crash-at",
        choices=("filter", "score", "bind"),
        help="diagnostic forced termination point before transaction commit",
    )

    status_parser = subparsers.add_parser(
        "status", help="print stable node, Pod, and injection state"
    )
    _add_common(status_parser, bundle_required=False)

    inject_parser = subparsers.add_parser(
        "inject", help="list validated injections and consumption status"
    )
    _add_common(inject_parser, bundle_required=False)
    inject_parser.add_argument("--id", dest="injection_id")

    verify_parser = subparsers.add_parser(
        "verify", help="replay the ledger and reconcile all resource debits"
    )
    _add_common(verify_parser, bundle_required=False)

    replay_parser = subparsers.add_parser(
        "replay",
        help="validate the immutable ledger and print all attribution entries",
    )
    _add_common(replay_parser, bundle_required=False)
    return parser


def _crash_hook(selected: str | None):
    def hook(phase: str) -> None:
        if selected == phase:
            os._exit(97)

    return hook


def _emit(result: dict[str, object], out: str | None) -> None:
    text = stable_json(result)
    _write_stdout(text)
    if out is not None:
        write_report(out, text)


def _write_stdout(text: str) -> None:
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8"))
        buffer.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "load":
            result = load_workspace(
                bundle_dir=args.bundle,
                db_path=args.db,
                reset=args.reset,
            )
        elif args.command == "schedule":
            result = schedule_workspace(
                bundle_dir=args.bundle,
                db_path=args.db,
                crash_hook=_crash_hook(args.crash_at),
            )
        elif args.command == "resume":
            result = resume_workspace(
                bundle_dir=args.bundle,
                db_path=args.db,
                crash_hook=_crash_hook(args.crash_at),
            )
        elif args.command == "status":
            result = status_workspace(
                bundle_dir=args.bundle,
                db_path=args.db,
            )
        elif args.command == "inject":
            result = inject_workspace(
                bundle_dir=args.bundle,
                db_path=args.db,
                injection_id=args.injection_id,
            )
        elif args.command == "verify":
            result = verify_workspace(
                bundle_dir=args.bundle,
                db_path=args.db,
            )
        elif args.command == "replay":
            result = replay_workspace(
                bundle_dir=args.bundle,
                db_path=args.db,
            )
        else:
            parser.error(f"unsupported command: {args.command}")
            return 2
    except BusinessError as exc:
        text = stable_json(exc.as_dict())
        _write_stdout(text)
        if args.out:
            write_report(args.out, text)
        return 2
    _emit(result, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
