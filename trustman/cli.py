"""Interactive command-line interface with stable exit codes."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

from .atomic import read_json_file
from .checkpoint import CheckpointStore
from .engine import (
    import_snapshots,
    inspect_checkpoint,
    report_tasks,
    run_workspace_validation,
)
from .errors import (
    E_ARGS,
    E_INTERNAL,
    E_LOCK,
    E_NOT_FOUND,
    E_REPORT_CORRUPT,
    LockConflict,
)
from .scheduler import SchedulerConfig


EXIT_SUCCESS = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_ARGUMENT_ERROR = 2


class CliUsageError(Exception):
    pass


def _print(value: Any) -> None:
    sys.stdout.write(
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    )


def _failure(code: str, message: str, exit_code: int) -> int:
    _print({"ok": False, "code": code, "message": message})
    return exit_code


def _check_files(paths: Iterable[str], label: str) -> list[str]:
    checked: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            raise CliUsageError(f"{label} file does not exist: {path}")
        checked.append(str(path))
    return checked


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trustman",
        description=(
            "Offline deterministic certificate-chain trust and checkpoint manager"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import-snapshot",
        help="atomically merge one or more trust-anchor snapshots",
    )
    import_parser.add_argument("--workspace", required=True)
    import_parser.add_argument(
        "--snapshot",
        dest="snapshots",
        action="append",
        required=True,
        help="snapshot JSON file; repeat for multiple files",
    )

    validate_parser = subparsers.add_parser(
        "validate",
        help="build and validate all declared chains",
    )
    validate_parser.add_argument("--workspace", required=True)
    validate_parser.add_argument(
        "--certificate",
        dest="certificates",
        action="append",
        required=True,
        help="certificate JSON file; repeat for multiple files",
    )
    validate_parser.add_argument(
        "--chain",
        dest="chains",
        action="append",
        required=True,
        help="chain-declaration JSON file; repeat for multiple files",
    )
    validate_parser.add_argument(
        "--snapshot",
        dest="snapshots",
        action="append",
        default=[],
        help="optional snapshot JSON file to merge before validation",
    )
    validate_parser.add_argument("--workers", type=int, default=4)
    validate_parser.add_argument("--max-tasks", type=int, default=1_000_000)
    validate_parser.add_argument("--queue-limit", type=int, default=1_000_000)
    validate_parser.add_argument(
        "--task-timeout-ms",
        type=int,
        default=None,
        help="global per-task wall-clock override (chain deadline still applies)",
    )
    validate_parser.add_argument(
        "--report",
        default=None,
        help="report path (default: <workspace>/report.json)",
    )
    validate_parser.add_argument(
        "--no-resume",
        action="store_true",
        help="ignore any existing checkpoint and rebuild",
    )

    query_parser = subparsers.add_parser(
        "query",
        help="query a chain conclusion from the last report or checkpoint",
    )
    query_parser.add_argument("--workspace", required=True)
    query_parser.add_argument("--chain-id", required=True)
    query_parser.add_argument("--report", default=None)

    checkpoint_parser = subparsers.add_parser(
        "check-checkpoint",
        help="validate checkpoint structure, checksum, policy and watermark",
    )
    checkpoint_parser.add_argument("--workspace", required=True)
    return parser


def _resolve_report_path(workspace: str, report: str | None) -> Path:
    if report is not None:
        return Path(report)
    return Path(workspace) / "report.json"


def _command_import(args: argparse.Namespace) -> int:
    snapshots = _check_files(args.snapshots, "snapshot")
    _, result = import_snapshots(
        workspace=args.workspace, snapshot_paths=snapshots
    )
    _print(result)
    return EXIT_SUCCESS if result["ok"] else EXIT_PARTIAL_FAILURE


def _command_validate(args: argparse.Namespace) -> int:
    certificates = _check_files(args.certificates, "certificate")
    chains = _check_files(args.chains, "chain")
    snapshots = _check_files(args.snapshots, "snapshot")
    try:
        config = SchedulerConfig(
            workers=args.workers,
            max_tasks=args.max_tasks,
            queue_limit=args.queue_limit,
            task_timeout_ms=args.task_timeout_ms,
        )
    except ValueError as exc:
        raise CliUsageError(str(exc)) from exc
    report_path = _resolve_report_path(args.workspace, args.report)
    outcome = run_workspace_validation(
        workspace=args.workspace,
        certificate_paths=certificates,
        chain_paths=chains,
        snapshot_paths=snapshots,
        config=config,
        use_resume=not args.no_resume,
        report_path=report_path,
    )
    summary = outcome.report["summary"]
    all_tasks_succeeded = summary["tasks_total"] == summary["succeeded"]
    ok = (
        all_tasks_succeeded
        and summary["input_rejections"] == 0
        and summary["expected_mismatches"] == 0
    )
    _print(
        {
            "ok": ok,
            "command": "validate",
            "report": str(report_path),
            "summary": summary,
            "reconciliation": outcome.report["reconciliation"],
            "metadata": outcome.metadata.as_dict(),
        }
    )
    return EXIT_SUCCESS if ok else EXIT_PARTIAL_FAILURE


def _load_query_source(
    workspace: Path, report_path: Path, chain_id: str
) -> tuple[dict[str, Any] | None, str]:
    if report_path.is_file():
        try:
            report = read_json_file(report_path)
        except (OSError, UnicodeDecodeError, ValueError):
            raise CliUsageError("report is not valid JSON")
        if isinstance(report, dict):
            result = report_tasks(report).get(chain_id)
            if result is not None:
                return result, "report"
    loaded = CheckpointStore(workspace).load()
    if loaded.status == "ok" and loaded.payload is not None:
        results = loaded.payload.get("results")
        if isinstance(results, dict) and isinstance(results.get(chain_id), dict):
            return results[chain_id], "checkpoint"
    return None, "none"


def _command_query(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace)
    report_path = _resolve_report_path(args.workspace, args.report)
    try:
        result, source = _load_query_source(
            workspace, report_path, args.chain_id
        )
    except CliUsageError as exc:
        return _failure(E_REPORT_CORRUPT, str(exc), EXIT_PARTIAL_FAILURE)
    if result is None:
        return _failure(
            E_NOT_FOUND,
            f"chain '{args.chain_id}' has no available conclusion",
            EXIT_PARTIAL_FAILURE,
        )
    _print(
        {
            "ok": True,
            "command": "query",
            "chain_id": args.chain_id,
            "source": source,
            "result": result,
        }
    )
    return EXIT_SUCCESS


def _command_check_checkpoint(args: argparse.Namespace) -> int:
    result = inspect_checkpoint(args.workspace)
    _print(result)
    return EXIT_SUCCESS if result["ok"] else EXIT_PARTIAL_FAILURE


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "import-snapshot":
            return _command_import(args)
        if args.command == "validate":
            return _command_validate(args)
        if args.command == "query":
            return _command_query(args)
        if args.command == "check-checkpoint":
            return _command_check_checkpoint(args)
        return _failure(E_ARGS, "unknown command", EXIT_ARGUMENT_ERROR)
    except CliUsageError as exc:
        return _failure(E_ARGS, str(exc), EXIT_ARGUMENT_ERROR)
    except LockConflict as exc:
        return _failure(E_LOCK, str(exc), EXIT_PARTIAL_FAILURE)
    except Exception as exc:
        if os.environ.get("TRUSTMAN_DEBUG"):
            raise
        return _failure(
            E_INTERNAL,
            f"{exc.__class__.__name__}: {exc}",
            EXIT_PARTIAL_FAILURE,
        )
