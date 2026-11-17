"""Small local CLI for validation, runs, recovery and replay."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .declaration import load_declaration
from .errors import BusinessError
from .workbench import Workbench


def _print(value: Any) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=str,
        )
    )


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--declaration", default="samples/cluster.json")
    parser.add_argument("--ledger", default="artifacts/workbench/ledger.sqlite")
    parser.add_argument("--snapshots", default="artifacts/workbench/snapshots")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local ML inference elastic-scaling decision workbench"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate a declaration file")
    validate_parser.add_argument("--declaration", default="samples/cluster.json")

    run_parser = subparsers.add_parser("run", help="start and advance a new run")
    _common(run_parser)
    run_parser.add_argument("--limit", type=int, default=None)

    for command, help_text in (
        ("recover", "recover only complete contiguous minutes"),
        ("resume", "replay complete minutes, then rerun pending minutes"),
        ("replay", "verify a run field-by-field"),
        ("snapshot", "save a whole-run snapshot"),
    ):
        item = subparsers.add_parser(command, help=help_text)
        _common(item)
        item.add_argument("--run-id", default=None)

    restore_parser = subparsers.add_parser("restore", help="restore a snapshot")
    _common(restore_parser)
    restore_parser.add_argument("--snapshot-id", required=True)

    view_parser = subparsers.add_parser("view", help="print the current ledger view")
    _common(view_parser)

    gui_parser = subparsers.add_parser("gui", help="launch the Tkinter workbench")
    _common(gui_parser)
    gui_parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "validate":
            declaration = load_declaration(args.declaration)
            _print(
                {
                    "ok": True,
                    "cluster_name": declaration.cluster_name,
                    "declaration_version": declaration.declaration_version,
                    "fingerprint": declaration.fingerprint,
                    "total_minutes": declaration.total_minutes,
                    "services": [
                        {
                            "name": service.name,
                            "requests": list(service.requests),
                            "allocation_cap": service.allocation_cap,
                        }
                        for service in declaration.services
                    ],
                }
            )
            return 0

        if args.command == "gui":
            from .ui import main as gui_main

            gui_args = ["--declaration", args.declaration, "--ledger", args.ledger]
            gui_args += ["--snapshots", args.snapshots]
            if args.check:
                gui_args.append("--check")
            return gui_main(gui_args)

        workbench = Workbench(args.declaration, args.ledger, args.snapshots)
        try:
            if args.command == "run":
                registration = workbench.load_declaration()
                result = workbench.run_all(limit_minutes=args.limit)
                result.pop("view", None)
                result["registration"] = registration.to_dict()
                result["snapshots"] = workbench.list_snapshots()
                _print(result)
                return 0

            workbench.load_stored_declaration()
            if args.command == "view":
                _print(workbench.get_current_view())
                return 0
            if args.command == "restore":
                _print(workbench.restore_snapshot(args.snapshot_id))
                return 0
            run = workbench.get_run(args.run_id)
            if args.command == "recover":
                _print(workbench.recover(run["run_id"]))
                return 0
            if args.command == "resume":
                _print(workbench.resume(run["run_id"]))
                return 0
            if args.command == "replay":
                _print(workbench.replay(run["run_id"]))
                return 0
            if args.command == "snapshot":
                _print(workbench.save_snapshot(run["run_id"]))
                return 0
        finally:
            workbench.close()
    except BusinessError as exc:
        _print(exc.to_dict())
        return 2
    except Exception as exc:
        _print(
            {
                "ok": False,
                "code": "E_INTERNAL",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
        return 3
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
