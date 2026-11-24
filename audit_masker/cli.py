from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence

from . import __version__
from .locking import LockError, WorkspaceLock
from .service import WorkspaceService
from .util import atomic_write_json


class CliArgumentError(ValueError):
    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit-masker",
        description=(
            "Offline deterministic audit-event masking ledger. "
            "All commands emit one JSON object on stdout."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_events = subparsers.add_parser(
        "import-events",
        help="import one or more JSONL audit event streams",
    )
    import_events.add_argument("--workspace", required=True, type=Path)
    import_events.add_argument("--events", nargs="+", required=True, type=Path)
    import_events.add_argument(
        "--output",
        type=Path,
        help="optionally write the command JSON report to this path",
    )

    import_rules = subparsers.add_parser(
        "import-rules",
        help="import a rules snapshot and/or incremental rule patches",
    )
    import_rules.add_argument("--workspace", required=True, type=Path)
    import_rules.add_argument("--snapshot", type=Path)
    import_rules.add_argument(
        "--patch",
        action="append",
        default=[],
        type=Path,
        help="patch JSON file; repeat for out-of-order patch batches",
    )
    import_rules.add_argument(
        "--output",
        type=Path,
        help="optionally write the command JSON report to this path",
    )

    recompute_parser = subparsers.add_parser(
        "recompute",
        help="incrementally rejudge affected events (or force a full pass)",
    )
    recompute_parser.add_argument("--workspace", required=True, type=Path)
    recompute_parser.add_argument(
        "--full",
        action="store_true",
        help="rejudge every event, useful for acceptance comparisons",
    )
    recompute_parser.add_argument(
        "--output",
        type=Path,
        help="optionally write the command JSON report to this path",
    )

    export_parser = subparsers.add_parser(
        "export-report",
        help="export a structured diagnostic report and dependency graph",
    )
    export_parser.add_argument("--workspace", required=True, type=Path)
    export_parser.add_argument("--output", required=True, type=Path)
    export_parser.add_argument("--graph", type=Path)
    export_parser.add_argument("--truth", type=Path)
    export_parser.add_argument(
        "--output-command",
        type=Path,
        help="optionally write the command JSON report to this path",
    )

    checkpoint = subparsers.add_parser(
        "checkpoint",
        help="create, verify, or restore atomic checkpoints",
    )
    checkpoint_sub = checkpoint.add_subparsers(
        dest="checkpoint_command",
        required=True,
    )
    create_parser = checkpoint_sub.add_parser("create")
    create_parser.add_argument("--workspace", required=True, type=Path)
    create_parser.add_argument("--snapshot", type=Path)
    create_parser.add_argument("--output", type=Path)
    verify_parser = checkpoint_sub.add_parser("verify")
    verify_parser.add_argument("--workspace", required=True, type=Path)
    verify_parser.add_argument("--output", type=Path)
    restore_parser = checkpoint_sub.add_parser("restore")
    restore_parser.add_argument("--workspace", required=True, type=Path)
    restore_parser.add_argument("--output", type=Path)

    verify_full = subparsers.add_parser(
        "verify-full",
        help="compare the incremental ledger with a from-scratch decision pass",
    )
    verify_full.add_argument("--workspace", required=True, type=Path)
    verify_full.add_argument("--output", type=Path)
    return parser


def _write_optional(
    path: Optional[Path],
    payload: Any,
    workspace: Path,
) -> None:
    if path is None:
        return
    with WorkspaceLock(workspace):
        atomic_write_json(path, payload)


def _run(args: argparse.Namespace) -> dict:
    service = WorkspaceService(args.workspace)
    if args.command == "import-events":
        result = service.import_events(args.events)
        _write_optional(args.output, result, args.workspace)
        return result
    if args.command == "import-rules":
        if args.snapshot is None and not args.patch:
            raise CliArgumentError(
                "import-rules requires --snapshot and/or --patch"
            )
        result = service.import_rules(args.snapshot, args.patch)
        _write_optional(args.output, result, args.workspace)
        return result
    if args.command == "recompute":
        result = service.recompute(full=args.full)
        _write_optional(args.output, result, args.workspace)
        return result
    if args.command == "export-report":
        result = service.export_report(
            args.output,
            graph_path=args.graph,
            truth_path=args.truth,
        )
        _write_optional(args.output_command, result, args.workspace)
        return result
    if args.command == "checkpoint":
        if args.checkpoint_command == "create":
            result = service.create_checkpoint(args.snapshot)
            _write_optional(args.output, result, args.workspace)
            return result
        if args.checkpoint_command == "verify":
            result = service.verify_checkpoints()
            _write_optional(args.output, result, args.workspace)
            return result
        if args.checkpoint_command == "restore":
            result = service.restore_checkpoint()
            _write_optional(args.output, result, args.workspace)
            return result
    if args.command == "verify-full":
        result = service.verify_full_consistency()
        _write_optional(args.output, result, args.workspace)
        return result
    raise CliArgumentError(f"unknown command: {args.command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = _run(args)
    except LockError as exc:
        result = {
            "command": getattr(args, "command", "unknown"),
            "success": False,
            "exit_code": 1,
            "error": str(exc),
            "business_codes": {"E_LOCK": 1},
        }
    except CliArgumentError as exc:
        result = {
            "command": getattr(args, "command", "unknown"),
            "success": False,
            "exit_code": 2,
            "error": str(exc),
            "business_codes": {"E_ARGUMENT": 1},
        }
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        result = {
            "command": getattr(args, "command", "unknown"),
            "success": False,
            "exit_code": 1,
            "error": f"{type(exc).__name__}: {exc}",
            "business_codes": {"E_INTERNAL": 1},
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return int(result.get("exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
