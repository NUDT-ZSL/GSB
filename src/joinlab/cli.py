"""Command-line interface for the join-order and cost-model lab."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .acceptance import run_acceptance
from .catalog import load_catalog
from .cost import CostParams, enumerate_plans
from .errors import JoinLabError, SchemaError
from .injection import create_injection
from .logging_utils import BusinessLogger
from .queries import load_query_file
from .report import build_report, load_session_record
from .session import ExperimentSession
from .snapshot import restore_snapshot, save_snapshot
from .typesys import json_dumps_stable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = PROJECT_ROOT / "samples" / "catalog.json"
DEFAULT_QUERIES = PROJECT_ROOT / "samples" / "queries.json"
DEFAULT_LOG = PROJECT_ROOT / "artifacts" / "logs" / "business.jsonl"


def _params(value: str | None) -> CostParams:
    if value is None:
        return CostParams()
    raw = value
    if value.startswith("@"):
        path = Path(value[1:]).expanduser().resolve()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SchemaError(f"cannot read parameter file: {exc}", str(path)) from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"invalid parameter JSON: {exc.msg}", "cost_params") from exc
    return CostParams.from_mapping(document)


def _emit(value: Any, out: str | None = None) -> None:
    text = json_dumps_stable(value, pretty=True)
    if out:
        target = Path(out).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG), help="catalog JSON path")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES), help="query sample JSON path")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="structured JSONL error log path")
    parser.add_argument("--out", default=None, help="also write this command's JSON result to a file")


def _parse_scale(value: str | float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SchemaError("sample scale must be a number", "sample_scale") from exc


def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    catalog = load_catalog(args.catalog)
    queries = load_query_file(args.queries, catalog)
    return {
        "ok": True,
        "catalog": {
            "name": catalog.name,
            "schema_version": catalog.schema_version,
            "data_version": catalog.data_version,
            "table_count": len(catalog.tables),
            "sample_rows": sum(len(item.rows) for item in catalog.data),
        },
        "queries": [item.to_dict() for item in queries],
        "validation": {
            "table_names_unique": True,
            "join_columns_resolved": True,
            "sample_row_counts_match": True,
        },
    }


def command_parse(args: argparse.Namespace) -> dict[str, Any]:
    catalog = load_catalog(args.catalog)
    session = ExperimentSession(catalog)
    query = session.parse(args.sql)
    return {
        "ok": True,
        "query_id": query.query_id,
        "canonical_sql": query.canonical_sql,
        "tables": list(query.tables),
        "projection": [ref.render() for ref in query.projection],
    }


def _session_from_args(args: argparse.Namespace) -> ExperimentSession:
    catalog = load_catalog(args.catalog)
    return ExperimentSession(catalog, params=_params(args.params), sample_scale=_parse_scale(args.sample_scale))


def command_plans(args: argparse.Namespace) -> dict[str, Any]:
    session = _session_from_args(args)
    query = session.parse(args.sql)
    plans = enumerate_plans(query, session.catalog, session.params, session.sample_scale)
    return {"ok": True, **plans.to_dict()}


def command_run(args: argparse.Namespace) -> dict[str, Any]:
    session = _session_from_args(args)
    evaluation = session.evaluate(args.sql, name=args.name)
    result = {
        "ok": True,
        "name": evaluation.name,
        "versions": evaluation.versions.to_dict(),
        "sample_scale": evaluation.sample_scale,
        "cost_params": session.params.to_dict(),
        **evaluation.to_dict(include_rows=not args.no_rows, include_candidates=True),
    }
    return result


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    session = _session_from_args(args)
    evaluation = session.evaluate(args.sql, name=args.name)
    summary = evaluation.summary
    return {
        "ok": True,
        "query_id": summary["query_id"],
        "canonical_sql": summary["canonical_sql"],
        "versions": summary["versions"],
        "selected_plan_id": summary["selected_plan_id"],
        "fallback_plan_id": summary["fallback_plan_id"],
        "consistency": summary["consistency"],
        "result": {
            "row_count": summary["row_count"],
            "result_digest": summary["result_digest"],
            "stats_fingerprint": summary["stats_fingerprint"],
        },
        "observational_timings_excluded": True,
    }


def _evaluate_sql_list(session: ExperimentSession, args: argparse.Namespace) -> list[Any]:
    evaluations = []
    if args.sql:
        for index, sql in enumerate(args.sql, start=1):
            evaluations.append(session.evaluate(sql, name=f"cli-query-{index}"))
        return evaluations
    for item in load_query_file(args.queries, session.catalog):
        evaluations.append(session.evaluate(item.sql, name=item.name))
    return evaluations


def command_snapshot_save(args: argparse.Namespace) -> dict[str, Any]:
    session = _session_from_args(args)
    evaluations = _evaluate_sql_list(session, args)
    saved = save_snapshot(session, args.snapshot)
    return {
        "ok": True,
        **saved,
        "selected_plans": [
            {
                "name": evaluation.name,
                "query_id": evaluation.query.query_id,
                "plan_id": evaluation.selected_plan.plan_id,
                "result_digest": evaluation.execution.selected.digest,
            }
            for evaluation in evaluations
        ],
    }


def command_snapshot_load(args: argparse.Namespace) -> dict[str, Any]:
    current = None
    if args.current_catalog:
        current = ExperimentSession(load_catalog(args.current_catalog), sample_scale=args.current_sample_scale)
    restore = restore_snapshot(args.snapshot, current_session=current)
    latest = [
        {
            "name": entry.name,
            "query_id": entry.query.query_id,
            "selected_plan_id": entry.selected_plan.plan_id,
            "result_digest": entry.execution.selected.digest,
            "row_count": entry.execution.selected.row_count,
            "versions": entry.versions.to_dict(),
        }
        for entry in restore.session.latest_evaluations()
    ]
    return {"ok": True, **restore.to_dict(), "queries": latest}


def command_report(args: argparse.Namespace) -> dict[str, Any]:
    record = load_session_record(args.session)
    return {"ok": True, "report": build_report(record)}


def command_inject(args: argparse.Namespace) -> dict[str, Any]:
    artifact = create_injection(
        args.kind,
        args.source_root,
        args.out_dir,
        baseline_snapshot=args.baseline_snapshot,
    )
    BusinessLogger(args.log).write(
        "injection_detected",
        {
            "operation": "inject",
            "code": artifact.detected_code,
            "expected_code": artifact.expected_code,
            "path": artifact.detected_path,
            "injection_kind": artifact.kind,
            "input_only": True,
        },
    )
    return {"ok": True, **artifact.to_dict()}


def command_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    result = run_acceptance(PROJECT_ROOT, args.out_dir)
    return {
        "ok": True,
        "report_path": result["report_path"],
        "session_record_path": result["session_record_path"],
        "snapshot_path": result["snapshot_path"],
        "summary": result["report"]["summary"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="joinlab",
        description="Deterministic multi-table join order and cost-model lab",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate catalog, sample rows, and query references")
    _add_common(validate)
    validate.set_defaults(handler=command_validate)

    parse_cmd = subparsers.add_parser("parse", help="parse and normalize one query")
    _add_common(parse_cmd)
    parse_cmd.add_argument("--sql", required=True)
    parse_cmd.set_defaults(handler=command_parse)

    plans = subparsers.add_parser("plans", help="enumerate and rank candidate plans")
    _add_common(plans)
    plans.add_argument("--sql", required=True)
    plans.add_argument("--params", default=None, help="JSON object or @file of cost parameters")
    plans.add_argument("--sample-scale", default="1.0")
    plans.set_defaults(handler=command_plans)

    run_cmd = subparsers.add_parser("run", help="select a plan, execute it, and verify a fallback path")
    _add_common(run_cmd)
    run_cmd.add_argument("--sql", required=True)
    run_cmd.add_argument("--name", default=None)
    run_cmd.add_argument("--params", default=None)
    run_cmd.add_argument("--sample-scale", default="1.0")
    run_cmd.add_argument("--no-rows", action="store_true", help="omit materialized rows from output")
    run_cmd.set_defaults(handler=command_run)

    verify = subparsers.add_parser("verify", help="report cross-plan byte consistency for one query")
    _add_common(verify)
    verify.add_argument("--sql", required=True)
    verify.add_argument("--name", default=None)
    verify.add_argument("--params", default=None)
    verify.add_argument("--sample-scale", default="1.0")
    verify.set_defaults(handler=command_verify)

    snap_save = subparsers.add_parser("snapshot-save", help="execute sample queries and save a full session snapshot")
    _add_common(snap_save)
    snap_save.add_argument("--snapshot", required=True)
    snap_save.add_argument("--sql", action="append", default=None)
    snap_save.add_argument("--params", default=None)
    snap_save.add_argument("--sample-scale", default="1.0")
    snap_save.set_defaults(handler=command_snapshot_save)

    snap_load = subparsers.add_parser("snapshot-load", help="fully replace session state from a snapshot and replay it")
    snap_load.add_argument("--snapshot", required=True)
    snap_load.add_argument("--current-catalog", default=str(DEFAULT_CATALOG))
    snap_load.add_argument("--current-sample-scale", type=float, default=1.0)
    snap_load.add_argument("--log", default=str(DEFAULT_LOG))
    snap_load.add_argument("--out", default=None)
    snap_load.set_defaults(handler=command_snapshot_load)

    report_cmd = subparsers.add_parser("report", help="build a stable verification report from a session record")
    report_cmd.add_argument("--session", required=True)
    report_cmd.add_argument("--log", default=str(DEFAULT_LOG))
    report_cmd.add_argument("--out", default=None)
    report_cmd.set_defaults(handler=command_report)

    inject = subparsers.add_parser("inject", help="materialize one of the three file-only anomaly fixtures")
    inject.add_argument("--kind", required=True, choices=["skew", "type", "snapshot-version"])
    inject.add_argument("--source-root", default=str(PROJECT_ROOT / "samples"))
    inject.add_argument("--out-dir", required=True)
    inject.add_argument("--baseline-snapshot", default=None)
    inject.add_argument("--log", default=str(DEFAULT_LOG))
    inject.add_argument("--out", default=None)
    inject.set_defaults(handler=command_inject)

    acceptance = subparsers.add_parser("acceptance", help="run the complete local acceptance suite and write reports")
    acceptance.add_argument("--out-dir", default=str(PROJECT_ROOT / "artifacts" / "acceptance"))
    acceptance.add_argument("--log", default=str(DEFAULT_LOG))
    acceptance.add_argument("--out", default=None)
    acceptance.set_defaults(handler=command_acceptance)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = BusinessLogger(getattr(args, "log", DEFAULT_LOG))
    try:
        result = args.handler(args)
        _emit(result, args.out)
        return 0
    except JoinLabError as exc:
        logger.error(args.command, exc)
        _emit(exc.to_dict())
        return 2
    except (OSError, ValueError) as exc:
        error = SchemaError(str(exc), f"cli.{args.command}")
        logger.error(args.command, error)
        _emit(error.to_dict())
        return 2
    except Exception as exc:  # pragma: no cover - last-resort structured CLI guard
        error = JoinLabError(
            "E_CLI",
            f"unexpected command failure: {exc}",
            f"cli.{args.command}",
            {"exception_type": type(exc).__name__},
        )
        logger.error(args.command, error)
        _emit(error.to_dict())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
