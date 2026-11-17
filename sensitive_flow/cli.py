"""Interactive CLI for import, trace, edit validation, and export."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .checkpoint import CheckpointStore, restore, run_edit_session
from .engine import (
    TraceState,
    apply_edits,
    build_diagnostic_report,
    build_graph_data,
    build_state,
    extract_edit_items,
    extract_snapshot_items,
)
from .errors import (
    E_ARGUMENT,
    E_FORCED_TERMINATION,
    E_INPUT,
    ForcedTermination,
    TraceError,
)
from .storage import (
    Workspace,
    combine_edit_payloads,
    combine_snapshot_payloads,
    load_json_file,
)


class CliArgumentError(Exception):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliArgumentError(message)


def _write_stdout(value: Any) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")


def _write_stderr(value: Any) -> None:
    sys.stderr.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")


def _result(
    command: str,
    status: str,
    *,
    exit_code: int,
    report_path: str | None = None,
    report: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "command": command,
        "status": status,
        "exit_code": exit_code,
    }
    if report_path is not None:
        result["report"] = report_path
    if report is not None:
        result["summary"] = report["summary"]
        result["recovery"] = report["recovery"]
        result["state_hash"] = report["state_hash"]
    if errors:
        result["errors"] = errors
    if extra:
        result.update(extra)
    return result


def _append_external_errors(
    report: dict[str, Any], errors: list[dict[str, Any]]
) -> None:
    if not errors:
        return
    for index, error in enumerate(errors):
        report["rejections"].append(
            {
                "document_id": "<input>",
                "item_index": index,
                "entry_number": index + 1,
                "item_type": "input",
                "code": error.get("code", E_INPUT),
                "message": error.get("message", "input failure"),
                "details": dict(error.get("details", {})),
            }
        )
    codes: dict[str, int] = {}
    for rejection in report["rejections"]:
        codes[rejection["code"]] = codes.get(rejection["code"], 0) + 1
    for violation in [
        item for document in report["documents"] for item in document["violations"]
    ]:
        codes[violation["code"]] = codes.get(violation["code"], 0) + 1
    report["summary"]["codes"] = {key: codes[key] for key in sorted(codes)}
    report["summary"]["rejections"] = len(report["rejections"])
    report["status"] = "partial"


def _exit_for_report(report: dict[str, Any]) -> int:
    return 0 if report["status"] == "ok" else 1


def _prepare_workspace(raw_path: str) -> Workspace:
    workspace = Workspace(raw_path).ensure()
    workspace.check_capabilities()
    return workspace


def _require_sources(snapshots: list[Any]) -> None:
    if not snapshots:
        raise TraceError(E_ARGUMENT, "workspace has no imported snapshots")


def command_import(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace = _prepare_workspace(args.workspace)
    payloads: list[Any] = []
    input_errors: list[dict[str, Any]] = []
    for raw_path in args.snapshots:
        try:
            payloads.append(load_json_file(raw_path))
        except TraceError as exc:
            if exc.code == E_ARGUMENT:
                raise
            input_errors.append(exc.as_dict())

    new_snapshots: list[Any] = []
    for payload in payloads:
        try:
            new_snapshots.extend(extract_snapshot_items(payload))
        except TraceError as exc:
            input_errors.append(exc.as_dict())

    with workspace.lock:
        existing = [] if args.replace else workspace.read_snapshots()
        combined = existing + new_snapshots
        workspace.write_snapshots(combined)
        state = build_state(combined)
        report = build_diagnostic_report(
            state,
            {
                "mode": "import",
                "checkpoint_used": False,
                "codes": [],
                "details": {
                    "files": [str(path) for path in args.snapshots],
                    "replace": bool(args.replace),
                },
            },
        )
        _append_external_errors(report, input_errors)
        report_path = workspace.reports_dir / "import.json"
        workspace.write_report(report, report_path)

    exit_code = _exit_for_report(report)
    return (
        _result(
            "import",
            report["status"],
            exit_code=exit_code,
            report_path=str(report_path),
            report=report,
            errors=input_errors or None,
        ),
        exit_code,
    )


def _load_optional_edit_payloads(paths: Sequence[str] | None) -> list[Any]:
    if not paths:
        return []
    return [load_json_file(path) for path in paths]


def command_trace(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace = _prepare_workspace(args.workspace)
    with workspace.lock:
        snapshots = workspace.read_snapshots()
        _require_sources(snapshots)
        stored_edits = workspace.read_edits()
        override_payloads = _load_optional_edit_payloads(args.edits)
        if override_payloads:
            edits = combine_edit_payloads(override_payloads)
            edit_source = "command-line"
        else:
            edits = stored_edits
            edit_source = "workspace"
        recovery = restore(workspace, snapshots, edits, resume_edits=True)
        report = build_diagnostic_report(
            recovery.state,
            {
                **recovery.as_recovery_dict(),
                "details": {
                    **recovery.as_recovery_dict()["details"],
                    "edit_source": edit_source,
                },
            },
        )
        report_path = (
            Path(args.report)
            if args.report
            else workspace.reports_dir / "trace.json"
        )
        workspace.write_report(report, report_path)
    exit_code = _exit_for_report(report)
    return (
        _result(
            "trace",
            report["status"],
            exit_code=exit_code,
            report_path=str(report_path),
            report=report,
        ),
        exit_code,
    )


def command_edit(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.checkpoint_every < 1:
        raise TraceError(E_ARGUMENT, "--checkpoint-every must be at least 1")
    if args.crash_after is not None and args.crash_after < 0:
        raise TraceError(E_ARGUMENT, "--crash-after must be non-negative")

    workspace = _prepare_workspace(args.workspace)
    with workspace.lock:
        snapshots = workspace.read_snapshots()
        _require_sources(snapshots)
        if args.edits:
            payloads = _load_optional_edit_payloads(args.edits)
            incoming_edits = combine_edit_payloads(payloads)
            if args.append:
                edits = workspace.read_edits() + incoming_edits
            else:
                edits = incoming_edits
            workspace.write_edits(edits)
        else:
            edits = workspace.read_edits()

        recovery = restore(workspace, snapshots, edits, resume_edits=False)
        store = CheckpointStore(workspace)
        store.save(recovery.state, lock=workspace.lock)
        try:
            outcomes = run_edit_session(
                recovery.state,
                edits,
                store,
                workspace.lock,
                checkpoint_every=args.checkpoint_every,
                crash_after=args.crash_after,
            )
        except ForcedTermination:
            raise

        report = build_diagnostic_report(
            recovery.state,
            {
                **recovery.as_recovery_dict(),
                "details": {
                    **recovery.as_recovery_dict()["details"],
                    "command_outcomes": outcomes,
                },
            },
        )
        report_path = workspace.reports_dir / "edit.json"
        workspace.write_report(report, report_path)

    exit_code = _exit_for_report(report)
    return (
        _result(
            "edit",
            report["status"],
            exit_code=exit_code,
            report_path=str(report_path),
            report=report,
            extra={"new_outcomes": outcomes},
        ),
        exit_code,
    )


def command_export(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace = _prepare_workspace(args.workspace)
    with workspace.lock:
        snapshots = workspace.read_snapshots()
        _require_sources(snapshots)
        edits = workspace.read_edits()
        recovery = restore(workspace, snapshots, edits, resume_edits=True)
        report = build_diagnostic_report(
            recovery.state, recovery.as_recovery_dict()
        )
        graph = build_graph_data(recovery.state)
        report_path = (
            Path(args.report)
            if args.report
            else workspace.reports_dir / "diagnostics.json"
        )
        graph_path = (
            Path(args.graph)
            if args.graph
            else workspace.reports_dir / "flow_graph.json"
        )
        workspace.write_report(report, report_path)
        workspace.write_graph(graph, graph_path)
    exit_code = _exit_for_report(report)
    return (
        _result(
            "export",
            report["status"],
            exit_code=exit_code,
            report_path=str(report_path),
            report=report,
            extra={"graph": str(graph_path)},
        ),
        exit_code,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="sensitive-flow",
        description="Offline sensitive-field lineage and edit validation workbench",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=JsonArgumentParser
    )

    import_parser = subparsers.add_parser("import", help="import snapshot JSON")
    import_parser.add_argument("--workspace", required=True)
    import_parser.add_argument("--snapshots", nargs="+", required=True)
    import_parser.add_argument(
        "--replace", action="store_true", help="replace existing snapshot source"
    )
    import_parser.set_defaults(handler=command_import)

    trace_parser = subparsers.add_parser("trace", help="analyze current workspace")
    trace_parser.add_argument("--workspace", required=True)
    trace_parser.add_argument(
        "--edits", nargs="+", help="temporary edit file(s) instead of stored edits"
    )
    trace_parser.add_argument("--report", help="trace report output path")
    trace_parser.set_defaults(handler=command_trace)

    edit_parser = subparsers.add_parser("edit", help="apply and validate edits")
    edit_parser.add_argument("--workspace", required=True)
    edit_parser.add_argument("--edits", nargs="+", help="edit file(s)")
    edit_parser.add_argument(
        "--append",
        action="store_true",
        help="append incoming edits instead of replacing stored edits",
    )
    edit_parser.add_argument("--checkpoint-every", type=int, default=1)
    edit_parser.add_argument(
        "--crash-after",
        type=int,
        default=None,
        help="simulate forced termination after this processed edit count",
    )
    edit_parser.set_defaults(handler=command_edit)

    export_parser = subparsers.add_parser("export", help="export report and flow graph")
    export_parser.add_argument("--workspace", required=True)
    export_parser.add_argument("--report", help="diagnostic JSON output path")
    export_parser.add_argument("--graph", help="flow graph JSON output path")
    export_parser.set_defaults(handler=command_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except CliArgumentError as exc:
        _write_stderr(
            {
                "status": "error",
                "exit_code": 2,
                "errors": [{"code": E_ARGUMENT, "message": str(exc)}],
            }
        )
        return 2

    try:
        result, exit_code = args.handler(args)
    except ForcedTermination as exc:
        _write_stderr(
            {
                "status": "terminated",
                "exit_code": 137,
                "errors": [
                    {
                        "code": E_FORCED_TERMINATION,
                        "message": str(exc),
                        "details": {
                            "checkpoint_edit_count": exc.checkpoint_edit_count
                        },
                    }
                ],
            }
        )
        return 137
    except TraceError as exc:
        exit_code = 2 if exc.code == E_ARGUMENT else 1
        _write_stderr(
            {
                "status": "error",
                "exit_code": exit_code,
                "errors": [exc.as_dict()],
            }
        )
        return exit_code
    _write_stdout(result)
    return exit_code


def entrypoint() -> None:
    raise SystemExit(main())
