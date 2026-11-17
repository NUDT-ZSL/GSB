from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .engine import AuditEngine
from .reporting import (
    case_summary,
    failures_from,
    issue_severity,
    report_status,
)
from .rules import RuleConfig, RuleConfigError, build_cases, load_rule_config
from .store import LoadResult, StoreError, Workspace, atomic_write_json
from .util import canonical_bytes

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_ARGUMENT = 2


def _json_output(value: Any, stream: Any) -> None:
    stream.write(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    stream.flush()


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        report = {
            "schema": "audit-forge.report/v1",
            "command": self.prog.split()[-1],
            "status": "error",
            "exit_code": EXIT_ARGUMENT,
            "errors": [
                {
                    "code": "E_ARGS",
                    "message": message,
                }
            ],
        }
        _json_output(report, sys.stderr)
        raise SystemExit(EXIT_ARGUMENT)


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="audit-forge",
        description="Offline deterministic audit event correlation and forensics",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import",
        help="import a JSONL audit stream",
    )
    import_parser.add_argument("--workspace", required=True)
    import_parser.add_argument("--input", required=True)
    import_parser.add_argument("--rules")
    import_parser.add_argument(
        "--checkpoint-every",
        type=_nonnegative_int,
        default=0,
        help="persist every N valid events; 0 persists only at the end",
    )
    import_parser.add_argument("--report")

    query_parser = subparsers.add_parser(
        "query-case",
        help="query one case or list cases",
    )
    query_parser.add_argument("--workspace", required=True)
    query_parser.add_argument("--case-id")
    query_parser.add_argument("--rules")
    query_parser.add_argument("--report")

    export_parser = subparsers.add_parser(
        "export-report",
        help="export a full forensic JSON report",
    )
    export_parser.add_argument("--workspace", required=True)
    export_parser.add_argument("--output")
    export_parser.add_argument("--rules")
    export_parser.add_argument("--report")

    check_parser = subparsers.add_parser(
        "check-checkpoint",
        help="validate checkpoint and case snapshot integrity",
    )
    check_parser.add_argument("--workspace", required=True)
    check_parser.add_argument("--rules")
    check_parser.add_argument(
        "--repair",
        action="store_true",
        help="atomically rewrite stale derived state",
    )
    check_parser.add_argument("--report")
    return parser


def _base_report(command: str, workspace: str | Path) -> dict[str, Any]:
    return {
        "schema": "audit-forge.report/v1",
        "tool_version": __version__,
        "command": command,
        "workspace": str(workspace),
        "status": "ok",
        "exit_code": EXIT_OK,
        "errors": [],
    }


def _finish(
    report: dict[str, Any],
    failures: list[dict[str, Any]],
    report_path: str | None,
    *,
    fatal: bool = False,
) -> int:
    status, exit_code = report_status(failures, fatal=fatal)
    report["status"] = status
    report["exit_code"] = exit_code
    if report_path:
        try:
            atomic_write_json(Path(report_path), report)
        except OSError as exc:
            failure = _base_report(report.get("command", "unknown"), "")
            failure["errors"] = [
                {
                    "code": "E_REPORT_WRITE",
                    "message": "cannot write requested JSON report",
                    "path": report_path,
                    "detail": str(exc),
                }
            ]
            failure["status"] = "error"
            failure["exit_code"] = EXIT_PARTIAL
            _json_output(failure, sys.stderr)
            return EXIT_PARTIAL
    _json_output(report, sys.stdout)
    return exit_code


def _store_error_report(
    command: str,
    workspace: str | Path,
    error: StoreError,
) -> dict[str, Any]:
    report = _base_report(command, workspace)
    report["errors"] = [error.as_dict()]
    report["status"] = "error"
    report["exit_code"] = EXIT_PARTIAL
    return report


def _rule_error_report(
    command: str,
    workspace: str | Path,
    error: RuleConfigError,
) -> dict[str, Any]:
    report = _base_report(command, workspace)
    item = {"code": error.code, "message": str(error)}
    item.update(error.details)
    report["errors"] = [item]
    report["status"] = "error"
    report["exit_code"] = EXIT_PARTIAL
    return report


def _case_counts(engine: AuditEngine) -> dict[str, int]:
    analysis = engine.analyze()
    return {
        "unique_events": len(engine.events),
        "cases": len(engine.cases),
        "rejected_lines": len(engine.rejects),
        "anomalies": len(analysis["anomalies"]),
        "error_anomalies": sum(
            1
            for anomaly in analysis["anomalies"]
            if anomaly.get("severity", "error") == "error"
        ),
    }


def _load_issue_payload(
    load_result: LoadResult,
    generation_before: int | None = None,
) -> dict[str, Any]:
    return {
        "recovered_from_checkpoint": load_result.checkpoint_exists,
        "generation_before": (
            load_result.engine.generation
            if generation_before is None
            else generation_before
        ),
        "cases_source": load_result.cases_source,
        "requires_rebuild": load_result.requires_rebuild,
        "issues": load_result.issues,
        "checks": load_result.checks,
        "cases_snapshot_valid": load_result.engine.cases_snapshot_valid,
    }


def _cmd_import(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace_path = Path(args.workspace)
    input_path = Path(args.input)
    requested_rules: RuleConfig | None = None
    if args.rules:
        requested_rules = load_rule_config(args.rules)
    if not input_path.is_file():
        report = _base_report("import", workspace_path)
        report["errors"] = [
            {
                "code": "E_INPUT",
                "message": "input JSONL file does not exist",
                "path": str(input_path),
            }
        ]
        return report, _finish(report, report["errors"], args.report, fatal=True)

    workspace = Workspace(workspace_path)
    with workspace.lock():
        load_result = workspace.load(requested_rules)
        engine = load_result.engine
        generation_before = engine.generation
        saved_generations: list[int] = []

        def checkpoint(current_engine: AuditEngine) -> None:
            generation = workspace.save(current_engine, current_engine.rules)
            saved_generations.append(generation)

        try:
            with input_path.open("rb") as stream:
                import_result = engine.ingest_stream(
                    stream,
                    source=input_path.name,
                    checkpoint_every=args.checkpoint_every,
                    checkpoint_callback=checkpoint,
                )
        except OSError as exc:
            report = _base_report("import", workspace_path)
            report["errors"] = [
                {
                    "code": "E_INPUT",
                    "message": "cannot read input JSONL stream",
                    "path": str(input_path),
                    "detail": str(exc),
                }
            ]
            return report, _finish(report, report["errors"], args.report, fatal=True)

        generation_after = workspace.save(engine, engine.rules)
        analysis = engine.analyze()
        failures = failures_from(engine, load_result, include_samples=True)
        report = _base_report("import", workspace_path)
        report.update(
            {
                "input": str(input_path),
                "load": _load_issue_payload(load_result, generation_before),
                "generation_after": generation_after,
                "checkpoint_generations": saved_generations + [generation_after],
                "import": import_result,
                "rule_version": engine.rules.version,
                "rule_fingerprint": engine.rules.fingerprint,
                "counts": _case_counts(engine),
                "watermarks": {
                    "max_seq": engine.max_seq_watermarks,
                    "contiguous_seq": engine.contiguous_seq_watermarks,
                },
                "normalized_events": [
                    event.as_dict()
                    for event in sorted(
                        engine.events.values(),
                        key=lambda item: item.canonical_key,
                    )
                ],
                "rejects": list(engine.rejects.values()),
                "anomalies": analysis["anomalies"],
                "trace_analyses": analysis["trace_analyses"],
                "case_summaries": [case_summary(case) for case in engine.cases],
            }
        )
        report["errors"] = [
            issue
            for issue in load_result.issues
            if issue_severity(issue) == "error"
        ]
        return report, _finish(report, failures, args.report)


def _cmd_query_case(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace_path = Path(args.workspace)
    requested_rules = load_rule_config(args.rules) if args.rules else None
    workspace = Workspace(workspace_path)
    with workspace.lock():
        loaded = workspace.load(requested_rules)
        report = _base_report("query-case", workspace_path)
        report["load"] = _load_issue_payload(loaded)
        if not loaded.checkpoint_exists:
            failures = failures_from(loaded.engine, loaded, include_samples=False)
            failures.extend(
                issue
                for issue in loaded.issues
                if issue_severity(issue) == "error"
            )
            failures.append(
                {
                    "code": "E_NO_CHECKPOINT",
                    "message": "workspace has no checkpoint",
                }
            )
            report["errors"] = [
                issue
                for issue in loaded.issues
                if issue_severity(issue) == "error"
            ]
            report["errors"].append(
                {
                    "code": "E_NO_CHECKPOINT",
                    "message": "workspace has no checkpoint",
                }
            )
            return report, _finish(report, failures, args.report, fatal=True)

        if args.case_id:
            match = next(
                (
                    case
                    for case in loaded.engine.cases
                    if case["case_id"] == args.case_id
                ),
                None,
            )
            if match is None:
                error = {
                    "code": "E_CASE_NOT_FOUND",
                    "message": "case id was not found",
                    "case_id": args.case_id,
                }
                report["errors"] = [error]
                return report, _finish(report, [error], args.report, fatal=True)
            report["case"] = match
        else:
            report["cases"] = [
                case_summary(case) for case in loaded.engine.cases
            ]
        report["counts"] = _case_counts(loaded.engine)
        failures = failures_from(loaded.engine, loaded, include_samples=False)
        return report, _finish(report, failures, args.report)


def _cmd_export_report(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace_path = Path(args.workspace)
    requested_rules = load_rule_config(args.rules) if args.rules else None
    workspace = Workspace(workspace_path)
    with workspace.lock():
        loaded = workspace.load(requested_rules)
        engine = loaded.engine
        analysis = engine.analyze()
        failures = failures_from(engine, loaded, include_samples=True)
        report = _base_report("export-report", workspace_path)
        report.update(
            {
                "load": _load_issue_payload(loaded),
                "rules": engine.rules.as_dict(),
                "counts": _case_counts(engine),
                "stats": engine.stats,
                "events": [
                    event.as_dict()
                    for event in sorted(
                        engine.events.values(),
                        key=lambda item: item.canonical_key,
                    )
                ],
                "watermarks": {
                    "max_seq": engine.max_seq_watermarks,
                    "contiguous_seq": engine.contiguous_seq_watermarks,
                },
                "rejects": list(engine.rejects.values()),
                "anomalies": analysis["anomalies"],
                "trace_analyses": analysis["trace_analyses"],
                "cases": engine.cases,
            }
        )
        report["errors"] = [
            issue
            for issue in loaded.issues
            if issue_severity(issue) == "error"
        ]
        if not loaded.checkpoint_exists:
            failures = failures + report["errors"]
            no_checkpoint = {
                "code": "E_NO_CHECKPOINT",
                "message": "workspace has no checkpoint",
            }
            report["errors"].append(no_checkpoint)
            failures.append(no_checkpoint)
            report["output"] = args.output
            return report, _finish(
                report,
                failures,
                args.report,
                fatal=True,
            )
        status, exit_code = report_status(failures)
        report["status"] = status
        report["exit_code"] = exit_code
        if args.output:
            try:
                atomic_write_json(Path(args.output), report)
            except OSError as exc:
                error = {
                    "code": "E_REPORT_WRITE",
                    "message": "cannot write exported report",
                    "path": args.output,
                    "detail": str(exc),
                }
                report["errors"].append(error)
                failures.append(error)
            report["output"] = args.output
        return report, _finish(report, failures, args.report)


def _cmd_check_checkpoint(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace_path = Path(args.workspace)
    requested_rules = load_rule_config(args.rules) if args.rules else None
    workspace = Workspace(workspace_path)
    with workspace.lock():
        probe_ok = workspace.probe_write()
        loaded = workspace.load(requested_rules)
        engine = loaded.engine
        generation_before = engine.generation
        report = _base_report("check-checkpoint", workspace_path)
        failures: list[dict[str, Any]] = []
        if not probe_ok:
            error = {
                "code": "E_WORKSPACE_WRITE",
                "message": "workspace cannot write temporary files and rename them",
                "path": str(workspace.root),
            }
            failures.append(error)

        repaired = False
        generation_repaired = None
        repair_source_issues: list[dict[str, Any]] = []
        if loaded.checkpoint_exists and args.repair:
            repair_source_issues = list(loaded.issues)
            generation_repaired = workspace.save(engine, engine.rules)
            repaired = True
            loaded.cases_source = "snapshot"
            loaded.requires_rebuild = False
            loaded.engine.cases_snapshot_valid = True
            loaded.issues = [
                issue
                for issue in loaded.issues
                if issue["code"]
                not in {
                    "E_CASE_SNAPSHOT_STALE",
                    "E_CASE_SNAPSHOT_CORRUPT",
                    "E_RULE_VERSION_CHANGED",
                    "E_SCHEMA_VERSION_CHANGED",
                }
            ]
            loaded.checks["cases_snapshot"] = {
                "status": "repaired",
                "failures": [],
            }

        if not loaded.checkpoint_exists:
            failures.extend(
                issue
                for issue in loaded.issues
                if issue_severity(issue) == "error"
            )
            failures.append(
                {
                    "code": "E_NO_CHECKPOINT",
                    "message": "checkpoint does not exist",
                }
            )
        else:
            recomputed_cases = build_cases(
                engine.events.values(),
                engine.rules,
            )
            cases_match = canonical_bytes(recomputed_cases) == canonical_bytes(
                engine.cases
            )
            if not cases_match:
                failures.append(
                    {
                        "code": "E_CASES_MISMATCH",
                        "message": "persisted case snapshot does not match deterministic event recomputation",
                    }
                )
            failures.extend(
                issue
                for issue in loaded.issues
                if issue_severity(issue) == "error"
            )
            if loaded.requires_rebuild:
                failures.append(
                    {
                        "code": "E_CHECKPOINT_REBUILD_REQUIRED",
                        "message": "checkpoint versions require derived-state rebuild",
                    }
                )
            if loaded.cases_source == "rebuilt" and not repaired:
                failures.append(
                    {
                        "code": "E_CASES_NOT_CURRENT",
                        "message": "case snapshot was not current and was rebuilt in memory",
                    }
                )

        analysis = engine.analyze()
        report.update(
            {
                "load": _load_issue_payload(loaded, generation_before),
                "repair": {
                    "requested": args.repair,
                    "performed": repaired,
                    "generation": generation_repaired,
                    "source_issues": repair_source_issues,
                },
                "workspace_write_probe": probe_ok,
                "cases_recompute_matches": (
                    True if not loaded.checkpoint_exists else cases_match
                ),
                "counts": _case_counts(engine),
                "temp_files": workspace.stale_temp_files(),
                "trace_analysis_error_count": sum(
                    1
                    for anomaly in analysis["anomalies"]
                    if anomaly.get("severity", "error") == "error"
                ),
            }
        )
        report["errors"] = failures
        return report, _finish(report, failures, args.report)


def _dispatch(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], int]:
    if args.command == "import":
        return _cmd_import(args)
    if args.command == "query-case":
        return _cmd_query_case(args)
    if args.command == "export-report":
        return _cmd_export_report(args)
    if args.command == "check-checkpoint":
        return _cmd_check_checkpoint(args)
    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report, exit_code = _dispatch(args)
    except RuleConfigError as exc:
        report = _rule_error_report(args.command, args.workspace, exc)
        _json_output(report, sys.stdout)
        return EXIT_PARTIAL
    except StoreError as exc:
        report = _store_error_report(args.command, args.workspace, exc)
        _json_output(report, sys.stdout)
        return EXIT_PARTIAL
    except OSError as exc:
        report = _base_report(args.command, args.workspace)
        report["errors"] = [
            {
                "code": "E_IO",
                "message": "filesystem operation failed",
                "detail": str(exc),
            }
        ]
        report["status"] = "error"
        report["exit_code"] = EXIT_PARTIAL
        _json_output(report, sys.stdout)
        return EXIT_PARTIAL
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

