"""Command-line interface for the observatory telemetry workbench."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .engine import (
    WorkloadError,
    build_ledger,
    evaluate_workload,
    query_result,
    recheck_policy,
    validate_workload,
)
from .generator import generate_workload
from .sampling import apply_policy_patch
from .store import (
    ProcessingInterrupted,
    StoreError,
    TelemetryStore,
    apply_persistent_recheck,
    build_store_report,
    process_workload,
)
from .util import canonical_json, deep_copy, parse_time_us


DEFAULT_SEED = 20_261_128
DEFAULT_DB = "artifacts/observatory.sqlite3"


def _add_seed_and_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--seed",
        type=int,
        default=argparse.SUPPRESS,
        help=f"deterministic generator seed (default: {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit JSON (the default and only machine format)",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        default=argparse.SUPPRESS,
        help="write output to FILE instead of stdout",
    )
    parser.add_argument(
        "--load-workload",
        metavar="FILE",
        default=argparse.SUPPRESS,
        help="load a previously generated workload JSON instead of generating",
    )


def _add_time_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--from",
        "--start",
        "--from-us",
        "--start-us",
        dest="from_time",
        default=None,
        help="inclusive trace-start lower bound (microseconds or ISO-8601)",
    )
    parser.add_argument(
        "--to",
        "--end",
        "--to-us",
        "--end-us",
        dest="to_time",
        default=None,
        help="exclusive trace-start upper bound (microseconds or ISO-8601)",
    )


def _add_scope_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--job-id",
        action="append",
        default=None,
        help="job id filter; repeat or use comma-separated values",
    )
    parser.add_argument(
        "--project-id",
        action="append",
        default=None,
        help="project id filter; repeat or use comma-separated values",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Deterministic observatory telemetry restoration, sampling, querying, "
            "retention accounting, and SQLite recovery workbench."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Every command emits canonical JSON.  With no subcommand, the complete "
            "workload is evaluated as `run`."
        ),
    )
    _add_seed_and_output(parser)
    parser.add_argument("--generate-only", action="store_true", default=False)
    parser.add_argument("--dump-workload", metavar="FILE", default=None)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="full restoration, decisions, and traces")
    _add_seed_and_output(run)
    run.add_argument("--policy-file", default=None, help="apply a sampling-policy patch")

    generate = subparsers.add_parser("generate", help="emit the raw deterministic workload")
    _add_seed_and_output(generate)

    restore = subparsers.add_parser("restore", help="emit restored trees and diagnostics")
    _add_seed_and_output(restore)

    sample = subparsers.add_parser("sample", help="emit per-trace sampling decisions")
    _add_seed_and_output(sample)
    sample.add_argument("--policy-file", default=None, help="apply a sampling-policy patch")

    query = subparsers.add_parser("query", help="query retained traces and failure root causes")
    _add_seed_and_output(query)
    _add_scope_filters(query)
    _add_time_filters(query)
    query.add_argument(
        "--trace-id",
        action="append",
        default=None,
        help="trace id filter; repeat or use comma-separated values",
    )
    query.add_argument(
        "--include-dropped",
        action="store_true",
        help="include traces whose final sampling decision was DROP",
    )
    query.add_argument("--db", default=None, help="query a completed SQLite store")
    query.add_argument("--policy-file", default=None, help="apply a sampling-policy patch")

    ledger = subparsers.add_parser("ledger", help="emit per-job retained-volume accounting")
    _add_seed_and_output(ledger)
    _add_scope_filters(ledger)
    _add_time_filters(ledger)
    ledger.add_argument(
        "--now-us",
        "--as-of",
        dest="now_us",
        default=None,
        help="override the project as-of time for retention expiry checks",
    )
    ledger.add_argument("--db", default=None, help="account a completed SQLite store")
    ledger.add_argument("--policy-file", default=None, help="apply a sampling-policy patch")

    recheck = subparsers.add_parser(
        "recheck",
        help="apply a policy/retention change and prove incremental == full",
    )
    _add_seed_and_output(recheck)
    _add_scope_filters(recheck)
    _add_time_filters(recheck)
    recheck.add_argument("--patch", "--patch-file", dest="patch_file", default=None)
    recheck.add_argument("--head-rate-bps", type=int, default=None)
    recheck.add_argument("--head-sampling-rate", type=float, default=None)
    recheck.add_argument(
        "--duration-threshold-ms",
        "--tail-threshold-ms",
        dest="duration_threshold_ms",
        type=int,
        default=None,
    )
    error_group = recheck.add_mutually_exclusive_group()
    error_group.add_argument("--tail-errors", dest="tail_errors", action="store_true", default=None)
    error_group.add_argument("--no-tail-errors", dest="tail_errors", action="store_false")
    recheck.add_argument(
        "--retention-hours",
        action="append",
        default=None,
        metavar="JOB=HOURS",
        help="override one job retention period; repeat as needed",
    )
    recheck.add_argument(
        "--now-us",
        "--as-of",
        dest="now_us",
        default=None,
        help="override the project as-of time for ledger comparison",
    )
    recheck.add_argument(
        "--db",
        default=None,
        help="persist the recheck to an existing completed SQLite store",
    )

    persist = subparsers.add_parser(
        "persist",
        help="commit every trace through SQLite trace-level transactions",
    )
    _add_seed_and_output(persist)
    persist.add_argument("--db", "--db-path", dest="db", default=DEFAULT_DB)
    persist.add_argument("--reset", action="store_true", help="clear an existing store")
    persist.add_argument("--policy-file", default=None)
    persist.add_argument(
        "--interrupt-after",
        "--simulate-interrupt-after",
        dest="interrupt_after",
        type=int,
        default=None,
        help="commit N new traces, then exit 130 to exercise recovery",
    )
    persist.add_argument("--commit-delay-ms", type=int, default=0)
    persist.add_argument(
        "--include-storage",
        action="store_true",
        help="wrap the result with deterministic SQLite state metadata",
    )

    resume = subparsers.add_parser(
        "resume",
        help="resume an interrupted SQLite store and emit the complete result",
    )
    _add_seed_and_output(resume)
    resume.add_argument("--db", "--db-path", dest="db", default=DEFAULT_DB)
    resume.add_argument("--policy-file", default=None)
    resume.add_argument(
        "--interrupt-after",
        "--simulate-interrupt-after",
        dest="interrupt_after",
        type=int,
        default=None,
    )
    resume.add_argument("--commit-delay-ms", type=int, default=0)
    resume.add_argument(
        "--include-storage",
        action="store_true",
        help="wrap the result with deterministic SQLite state metadata",
    )

    return parser


def _split_values(values: Iterable[str] | None) -> list[str] | None:
    if not values:
        return None
    result: list[str] = []
    for value in values:
        for item in str(value).split(","):
            item = item.strip()
            if item:
                result.append(item)
    return sorted(set(result)) if result else None


def _load_json_file(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_workload(args: argparse.Namespace) -> dict[str, Any]:
    loaded = getattr(args, "load_workload", None)
    if loaded:
        workload = _load_json_file(loaded)
        validate_workload(workload)
        return workload
    seed = int(getattr(args, "seed", DEFAULT_SEED))
    return generate_workload(seed)


def _apply_policy_file(workload: dict[str, Any], path: str | None) -> dict[str, Any]:
    if not path:
        return workload
    patch = _load_json_file(path)
    return apply_policy_patch(workload, patch)


def _parse_retention_hours(values: Iterable[str] | None) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw in values or []:
        if "=" not in raw:
            raise ValueError("--retention-hours must use JOB=HOURS")
        job_id, hours = raw.split("=", 1)
        job_id = job_id.strip()
        if not job_id:
            raise ValueError("--retention-hours requires a non-empty job id")
        result[job_id] = int(round(float(hours.strip()) * 3_600_000_000))
    return result


def _build_recheck_patch(args: argparse.Namespace) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if args.patch_file:
        loaded = _load_json_file(args.patch_file)
        if not isinstance(loaded, dict):
            raise ValueError("policy patch file must contain a JSON object")
        patch.update(deep_copy(loaded))
    if args.head_rate_bps is not None:
        patch["head_sampling_rate_basis_points"] = args.head_rate_bps
    if args.head_sampling_rate is not None:
        patch["head_sampling_rate"] = args.head_sampling_rate
    if args.duration_threshold_ms is not None:
        patch["tail_duration_threshold_ms"] = args.duration_threshold_ms
    if args.tail_errors is not None:
        patch["tail_error_enabled"] = bool(args.tail_errors)
    retention = _parse_retention_hours(args.retention_hours)
    if retention:
        existing = dict(patch.get("retention_overrides_us", {}))
        existing.update(retention)
        patch["retention_overrides_us"] = existing
    if not patch:
        raise ValueError(
            "recheck requires --patch or at least one policy/retention option"
        )
    return patch


def _time_args(args: argparse.Namespace) -> tuple[int | None, int | None, int | None]:
    from_us = parse_time_us(getattr(args, "from_time", None))
    to_us = parse_time_us(getattr(args, "to_time", None))
    now_us = parse_time_us(getattr(args, "now_us", None))
    return from_us, to_us, now_us


def _result_for_args(
    args: argparse.Namespace,
    workload: dict[str, Any],
    *,
    policy_file: str | None = None,
) -> tuple[dict[str, Any], TelemetryStore | None]:
    db = getattr(args, "db", None)
    if db:
        store = TelemetryStore(str(db))
        try:
            store.initialize(workload)
            report = build_store_report(store, workload)
        except BaseException:
            store.close()
            raise
        return report["result"], store
    adjusted = _apply_policy_file(workload, policy_file)
    return evaluate_workload(adjusted), None


def _write_output(value: Any, output: str | None) -> None:
    text = canonical_json(value)
    if output and output != "-":
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _project_restoration(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": "observatory-span-restoration",
        "projects": [
            {
                "diagnostics": deep_copy(project["diagnostics"]),
                "edge_cases": deep_copy(project["edge_cases"]),
                "jobs": deep_copy(project["jobs"]),
                "project_id": project["project_id"],
                "summary": {
                    "diagnostic_count": len(project["diagnostics"]),
                    "span_count": project["summary"]["span_count"],
                    "trace_count": project["summary"]["trace_count"],
                },
                "traces": [
                    {
                        "failure_analysis": deep_copy(trace["failure_analysis"]),
                        "job_id": trace["job_id"],
                        "job_kind": trace["job_kind"],
                        "observation_id": trace["observation_id"],
                        "restoration": deep_copy(trace["restoration"]),
                        "trace_id": trace["trace_id"],
                    }
                    for trace in project["traces"]
                ],
            }
            for project in result["projects"]
        ],
        "seed": result["seed"],
        "semantics": deep_copy(result["semantics"]),
        "version": "1.0",
        "workload_sha256": result["workload_sha256"],
    }


def _project_sampling(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": "observatory-sampling-decisions",
        "projects": [
            {
                "policy": deep_copy(project["policy"]),
                "project_id": project["project_id"],
                "summary": {
                    "dropped_trace_count": project["summary"]["dropped_trace_count"],
                    "head_kept_trace_count": project["summary"]["head_kept_trace_count"],
                    "retained_trace_count": project["summary"]["retained_trace_count"],
                    "tail_kept_trace_count": project["summary"]["tail_kept_trace_count"],
                    "tail_rescued_trace_count": project["summary"][
                        "tail_rescued_trace_count"
                    ],
                    "trace_count": project["summary"]["trace_count"],
                },
                "traces": [
                    {
                        "failure_analysis": deep_copy(trace["failure_analysis"]),
                        "job_id": trace["job_id"],
                        "job_kind": trace["job_kind"],
                        "sample_decision": deep_copy(trace["sample_decision"]),
                        "trace_id": trace["trace_id"],
                    }
                    for trace in project["traces"]
                ],
            }
            for project in result["projects"]
        ],
        "seed": result["seed"],
        "semantics": deep_copy(result["semantics"]),
        "version": "1.0",
        "workload_sha256": result["workload_sha256"],
    }


def _persist_or_resume(args: argparse.Namespace, workload: dict[str, Any]) -> tuple[Any, int]:
    db = str(args.db)
    with TelemetryStore(db) as store:
        store.initialize(workload, reset=bool(getattr(args, "reset", False)))
        history = store.policy_history()
        policy_file = getattr(args, "policy_file", None)
        if policy_file:
            if history or store.committed_count():
                raise StoreError(
                    "--policy-file is only accepted for a new/reset store; "
                    "use the `recheck --db` command for an existing store"
                )
            initial_patch = _load_json_file(policy_file)
            store.append_policy_patch(initial_patch)
            history = store.policy_history()

        adjusted = deep_copy(workload)
        for patch in history:
            adjusted = apply_policy_patch(adjusted, patch)
        try:
            progress = process_workload(
                store,
                adjusted,
                interrupt_after=getattr(args, "interrupt_after", None),
                commit_delay_ms=int(getattr(args, "commit_delay_ms", 0)),
            )
        except ProcessingInterrupted as exc:
            sys.stderr.write(
                canonical_json(
                    {
                        "committed_before_interrupt": exc.committed_count,
                        "interrupted": True,
                        "status": "resume_required",
                    }
                )
            )
            return None, 130
        report = build_store_report(
            store,
            workload,
            newly_committed=progress["newly_committed_trace_count"],
            skipped_committed=progress["skipped_committed_trace_count"],
        )
        if getattr(args, "include_storage", False):
            return report, 0
        return report["result"], 0


def _handle(args: argparse.Namespace) -> int:
    command = args.command
    if args.generate_only and command in (None, "run"):
        command = "generate"
    if command is None:
        command = "run"
    workload = _load_workload(args)

    if args.dump_workload:
        _write_output(workload, args.dump_workload)

    if command == "generate":
        _write_output(workload, getattr(args, "output", None))
        return 0

    if command == "run":
        adjusted = _apply_policy_file(workload, getattr(args, "policy_file", None))
        _write_output(evaluate_workload(adjusted), getattr(args, "output", None))
        return 0

    if command in {"sample", "restore"}:
        adjusted = _apply_policy_file(workload, getattr(args, "policy_file", None))
        result = evaluate_workload(adjusted)
        output = (
            _project_sampling(result)
            if command == "sample"
            else _project_restoration(result)
        )
        _write_output(output, getattr(args, "output", None))
        return 0

    if command in {"query", "ledger"}:
        policy_file = getattr(args, "policy_file", None)
        result, store = _result_for_args(args, workload, policy_file=policy_file)
        try:
            from_us, to_us, now_us = _time_args(args)
            job_ids = _split_values(getattr(args, "job_id", None))
            project_ids = _split_values(getattr(args, "project_id", None))
            if command == "query":
                output = query_result(
                    result,
                    trace_ids=_split_values(getattr(args, "trace_id", None)),
                    job_ids=job_ids,
                    project_ids=project_ids,
                    from_us=from_us,
                    to_us=to_us,
                    include_dropped=bool(getattr(args, "include_dropped", False)),
                )
            else:
                output = build_ledger(
                    result,
                    job_ids=job_ids,
                    project_ids=project_ids,
                    from_us=from_us,
                    to_us=to_us,
                    now_us_override=now_us,
                )
            _write_output(output, getattr(args, "output", None))
            return 0
        finally:
            if store is not None:
                store.close()

    if command == "recheck":
        patch = _build_recheck_patch(args)
        from_us, to_us, now_us = _time_args(args)
        job_ids = _split_values(getattr(args, "job_id", None))
        project_ids = _split_values(getattr(args, "project_id", None))
        db = getattr(args, "db", None)
        if db:
            with TelemetryStore(str(db)) as store:
                store.initialize(workload)
                output = apply_persistent_recheck(
                    store,
                    workload,
                    patch,
                    job_ids=job_ids,
                    project_ids=project_ids,
                    from_us=from_us,
                    to_us=to_us,
                    now_us_override=now_us,
                )
        else:
            base_result = evaluate_workload(workload)
            output = recheck_policy(
                workload,
                patch,
                base_result=base_result,
                job_ids=job_ids,
                project_ids=project_ids,
                from_us=from_us,
                to_us=to_us,
                now_us_override=now_us,
            )
        _write_output(output, getattr(args, "output", None))
        return 0

    if command in {"persist", "resume"}:
        output, exit_code = _persist_or_resume(args, workload)
        if output is not None:
            _write_output(output, getattr(args, "output", None))
        return exit_code

    raise ValueError(f"unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _handle(args)
    except KeyboardInterrupt:
        sys.stderr.write(
            canonical_json(
                {
                    "interrupted": True,
                    "status": "resume_required",
                }
            )
        )
        return 130
    except (WorkloadError, StoreError, ValueError, sqlite3.Error) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
if __name__ == "__main__":
    raise SystemExit(main())

