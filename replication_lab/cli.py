"""Command-line entry point for the replication delay workbench."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import LabError, ParameterError
from .reports import (
    render_json_report,
    render_observation_report,
    render_recovery_report,
    render_verification_report,
)
from .service import LabSession, load_operations_file
from .util import stable_json


def _default_root() -> Path:
    return Path.cwd() / "lab-data"


def _add_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        default=_default_root(),
        help="local lab directory (default: ./lab-data)",
    )


def _add_replay_parameters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rate", type=int, default=10, help="replay transactions/second")
    parser.add_argument("--concurrency", type=int, default=2, help="concurrent replay degree")
    parser.add_argument("--reset", action="store_true", help="discard persisted replay state")
    parser.add_argument("--json", action="store_true", help="emit JSON")


def _emit(value: Any, *, as_json: bool, text: str | None = None) -> None:
    if as_json:
        print(render_json_report(value), end="")
    elif text is not None:
        print(text, end="")
    else:
        print(stable_json(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replab",
        description="Local versioned master/replica delay observation workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    write = subparsers.add_parser("write", help="append deterministic operations in current v3")
    _add_root(write)
    write.add_argument("--ops", type=Path, required=True)
    write.add_argument("--per-segment", type=int, default=4)
    write.add_argument("--json", action="store_true")

    observe = subparsers.add_parser("observe", help="run or resume replay and estimate delay")
    _add_root(observe)
    _add_replay_parameters(observe)

    recover = subparsers.add_parser("recover", help="recover persisted state only")
    _add_root(recover)
    recover.add_argument("--json", action="store_true")

    upgrade = subparsers.add_parser("upgrade", help="detect and upgrade legacy segments")
    _add_root(upgrade)
    upgrade.add_argument("--json", action="store_true")

    report = subparsers.add_parser("report", help="show current observation report")
    _add_root(report)
    report.add_argument("--json", action="store_true")

    snapshot_save = subparsers.add_parser("snapshot-save", help="persist a whole observation snapshot")
    _add_root(snapshot_save)
    snapshot_save.add_argument("--json", action="store_true")

    snapshot_list = subparsers.add_parser("snapshot-list", help="list snapshots in stable order")
    _add_root(snapshot_list)
    snapshot_list.add_argument("--json", action="store_true")

    snapshot_restore = subparsers.add_parser(
        "snapshot-restore", help="restore a snapshot as a whole view"
    )
    _add_root(snapshot_restore)
    snapshot_restore.add_argument("snapshot_id")
    snapshot_restore.add_argument("--json", action="store_true")

    snapshot_verify = subparsers.add_parser(
        "snapshot-verify", help="replay a snapshot and compare every recorded item"
    )
    _add_root(snapshot_verify)
    snapshot_verify.add_argument("snapshot_id")
    snapshot_verify.add_argument("--rate", type=int, default=None)
    snapshot_verify.add_argument("--concurrency", type=int, default=None)
    snapshot_verify.add_argument("--json", action="store_true")

    snapshot_diff = subparsers.add_parser("snapshot-diff", help="stable diff between two snapshots")
    _add_root(snapshot_diff)
    snapshot_diff.add_argument("left_snapshot_id")
    snapshot_diff.add_argument("right_snapshot_id")
    snapshot_diff.add_argument("--json", action="store_true")

    demo = subparsers.add_parser("demo", help="run all three deterministic demonstration scenarios")
    demo.add_argument("--output", type=Path, default=Path.cwd() / "artifacts" / "demo")

    gui = subparsers.add_parser("gui", help="launch the Tkinter workbench")
    _add_root(gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "demo":
        from .demo import run_demo

        result = run_demo(args.output)
        print(result["rendered_report"], end="")
        return 0 if result["ok"] else 2
    if args.command == "gui":
        from .gui import run_gui

        try:
            run_gui(args.root)
            return 0
        except LabError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    try:
        if args.command == "write":
            operations = load_operations_file(args.ops)
            with LabSession.open(args.root) as session:
                result = session.write_operations(
                    operations, records_per_segment=args.per_segment
                )
            _emit(result, as_json=args.json)
            return 0

        if args.command == "observe":
            with LabSession.open(args.root) as session:
                session.replay(
                    replay_rate=args.rate,
                    concurrency=args.concurrency,
                    reset=args.reset,
                )
                report = session.observation_report()
            _emit(
                report,
                as_json=args.json,
                text=render_observation_report(report),
            )
            return 0

        if args.command == "recover":
            with LabSession.open(args.root) as session:
                result = session.recover()
            _emit(
                result,
                as_json=args.json,
                text=render_recovery_report(result),
            )
            return 0

        if args.command == "upgrade":
            with LabSession.open(args.root, auto_upgrade=False) as session:
                result = session.upgrade_all()
            _emit(result, as_json=args.json)
            return 0 if not result.get("failed") else 2

        if args.command == "report":
            with LabSession.open(args.root) as session:
                report = session.observation_report()
            _emit(report, as_json=args.json, text=render_observation_report(report))
            return 0

        if args.command == "snapshot-save":
            with LabSession.open(args.root) as session:
                result = session.save_snapshot()
            _emit(result, as_json=args.json)
            return 0

        if args.command == "snapshot-list":
            with LabSession.open(args.root, auto_upgrade=False) as session:
                result = session.list_snapshots()
            _emit(
                {"snapshots": result},
                as_json=args.json,
                text=stable_json({"snapshots": result}) + "\n",
            )
            return 0

        if args.command == "snapshot-restore":
            with LabSession.open(args.root, auto_upgrade=False) as session:
                result = session.restore_snapshot(args.snapshot_id)
            _emit(result, as_json=args.json)
            return 0

        if args.command == "snapshot-verify":
            with LabSession.open(args.root, auto_upgrade=False) as session:
                result = session.verify_snapshot(
                    args.snapshot_id,
                    replay_rate=args.rate,
                    concurrency=args.concurrency,
                )
            _emit(
                result,
                as_json=args.json,
                text=render_verification_report(result),
            )
            return 0 if result.get("ok") else 2

        if args.command == "snapshot-diff":
            with LabSession.open(args.root, auto_upgrade=False) as session:
                result = session.snapshot_diff(
                    args.left_snapshot_id, args.right_snapshot_id
                )
            _emit(result, as_json=args.json)
            return 0
    except LabError as exc:
        if getattr(args, "json", False):
            print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"E_IO: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
