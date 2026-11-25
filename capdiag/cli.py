from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .errors import CapDiagError, ErrorCode
from .generate import generate_sample_workspace
from .invalidation import (
    metric_config_change_plan,
    recompute_evaluations_for_new_threshold,
    threshold_change_plan,
)
from .jsonutil import dump_json, dumps, load_json
from .locking import file_lock
from .model import Scenario
from .storage import connect, current_threshold, recover
from .threshold_store import apply_threshold_view, candidates_from_current_payload
from .thresholds import (
    load_v1_thresholds,
    load_v2_thresholds,
    merge_thresholds,
    validate_metric_references,
)
from .validation import load_metric_config, load_scenario, load_trace
from .verify import full_verify
from .versions import threshold_version
from .engine import run_service


ROOT = Path.cwd()
DEFAULT_DB = ROOT / "out" / "capdiag.sqlite"


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    scenario = Path(args.scenario)
    traces = Path(args.traces)
    metrics = Path(args.metrics)
    return scenario, traces, metrics


def _load_inputs(args: argparse.Namespace) -> tuple[Scenario, Any, dict[str, Any]]:
    scenario_path, trace_path, metrics_path = _paths(args)
    scenario = load_scenario(scenario_path)
    trace_bundle = load_trace(trace_path, scenario)
    metrics_config = load_metric_config(metrics_path)
    return scenario, trace_bundle, metrics_config


def _threshold_bundle(
    *,
    v1: str | Path | None,
    v2_metadata: str | Path | None,
    origin_ms: int = 0,
    metric_names: set[str] | None = None,
) -> dict[str, Any]:
    candidates = []
    if v1 is not None:
        candidates.extend(load_v1_thresholds(v1, origin_logical_ms=origin_ms))
    if v2_metadata is not None:
        candidates.extend(load_v2_thresholds(v2_metadata))
    if not candidates:
        raise CapDiagError(ErrorCode.SCHEMA, "at least one threshold source is required")
    if metric_names is not None:
        validate_metric_references(metric_names, candidates)
    unified = merge_thresholds(
        [c for c in candidates if c.source_version == "v1"],
        [c for c in candidates if c.source_version == "v2"],
    )
    version = threshold_version(unified)
    v2_candidates = [c for c in candidates if c.source_version == "v2"]
    primary = v2_candidates[0] if v2_candidates else max(candidates, key=lambda c: c.generation)
    return {
        "candidates": candidates,
        "candidates_payload": [c.to_dict() for c in candidates],
        "unified_view": unified,
        "threshold_version": version,
        "primary_snapshot_id": primary.snapshot_id,
        "generation": primary.generation,
        "effective_logical_ms": primary.effective_logical_ms,
        "source_version": primary.source_version,
    }


def cmd_generate(args: argparse.Namespace) -> int:
    result = generate_sample_workspace(args.root)
    dump_json(Path(args.root) / "out" / "generation-manifest.json", result)
    print(dumps({"ok": True, "generated": result}))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        scenario = load_scenario(args.scenario)
        traces = load_trace(args.traces, scenario)
        metrics = load_metric_config(args.metrics)
        threshold_info = None
        if args.v1 or args.v2_metadata:
            bundle = _threshold_bundle(
                v1=args.v1,
                v2_metadata=args.v2_metadata,
                origin_ms=scenario.origin_logical_ms,
                metric_names=set(metrics["metrics"]),
            )
            threshold_info = {
                "threshold_version": bundle["threshold_version"],
                "entry_count": bundle["unified_view"]["entry_count"],
            }
        report = {
            "ok": not traces.errors,
            "service_count": len(scenario.services),
            "instance_count": sum(len(s.instances) for s in scenario.services),
            "valid_trace_records": len(traces.records),
            "trace_error_count": len(traces.errors),
            "trace_errors": traces.errors,
            "metrics": sorted(metrics["metrics"]),
            "thresholds": threshold_info,
        }
        if args.json:
            dump_json(args.json, report)
        print(dumps(report if traces.errors else {k: v for k, v in report.items() if k != "trace_errors"}))
        return 0 if not traces.errors else 2
    except CapDiagError as exc:
        error = exc.to_dict()
        if args.json:
            dump_json(args.json, error)
        print(dumps(error), file=sys.stderr)
        return 2


def _resolve_thresholds_for_run(
    args: argparse.Namespace,
    conn: Any,
    scenario: Scenario,
    metrics_config: dict[str, Any],
) -> tuple[list[Any], str]:
    if args.v1 or args.v2_metadata:
        bundle = _threshold_bundle(
            v1=args.v1,
            v2_metadata=args.v2_metadata,
            origin_ms=scenario.origin_logical_ms,
            metric_names=set(metrics_config["metrics"]),
        )
        return bundle["candidates"], bundle["threshold_version"]
    payload = current_threshold(conn)
    if payload is None:
        raise CapDiagError(
            ErrorCode.SCHEMA,
            "no current threshold snapshot in database; pass --v1/--v2-metadata or run apply-threshold",
        )
    return candidates_from_current_payload(payload), payload["threshold_version"]


def cmd_run(args: argparse.Namespace) -> int:
    scenario, trace_bundle, metrics_config = _load_inputs(args)
    if trace_bundle.errors and args.strict_traces:
        raise CapDiagError(
            ErrorCode.SCHEMA,
            "trace validation failed",
            details={"errors": trace_bundle.errors[:20]},
        )

    conn = connect(args.db)
    try:
        candidates, thr_version = _resolve_thresholds_for_run(args, conn, scenario, metrics_config)
        services = [args.service] if args.service else [s.service_name for s in scenario.services]
        reports = []
        for service_name in services:
            scope_ids = {service_name}
            service = scenario.service(service_name)
            scope_ids.update(i.instance_id for i in service.instances)
            with file_lock(Path(args.db).parent / "locks", f"service-{service_name}"):
                recover(conn, scope_ids=scope_ids, include_thresholds=False)
                report = run_service(
                    conn=conn,
                    scenario=scenario,
                    records=trace_bundle.records,
                    metrics_config=metrics_config,
                    threshold_candidates=candidates,
                    threshold_version=thr_version,
                    service_name=service_name,
                    crash_at=args.crash_at,
                    crash_window=args.crash_window,
                )
                reports.append(report)

        budget_violations = [v for report in reports for v in report["budget_violations"]]
        output = {
            "ok": not budget_violations,
            "database": args.db,
            "threshold_version": thr_version,
            "trace_validation_errors": trace_bundle.errors,
            "reports": reports,
            "budget_violations": budget_violations,
        }
        if args.report:
            dump_json(args.report, output)
        summary = {
            "ok": output["ok"],
            "db": args.db,
            "services": len(reports),
            "committed": sum(len(r["committed_windows"]) for r in reports),
            "skipped": sum(len(r["skipped_windows"]) for r in reports),
            "budget_violations": len(budget_violations),
            "report": args.report,
        }
        print(dumps(summary))
        return 0 if not budget_violations else 3
    finally:
        conn.close()


def cmd_recover(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    try:
        result = recover(conn)
        if args.json:
            dump_json(args.json, result)
        print(dumps({"ok": True, "recovery": result}))
        return 0
    finally:
        conn.close()


def cmd_apply_threshold(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    metrics = load_metric_config(args.metrics)
    bundle = _threshold_bundle(
        v1=args.v1,
        v2_metadata=args.v2_metadata,
        origin_ms=scenario.origin_logical_ms,
        metric_names=set(metrics["metrics"]),
    )
    conn = connect(args.db)
    try:
        with file_lock(Path(args.db).parent / "locks", "threshold-replacement"):
            recover(conn, include_thresholds=True)
            result = apply_threshold_view(
                conn,
                unified_view=bundle["unified_view"],
                candidates_payload=bundle["candidates_payload"],
                threshold_version=bundle["threshold_version"],
                primary_snapshot_id=bundle["primary_snapshot_id"],
                generation=bundle["generation"],
                effective_logical_ms=bundle["effective_logical_ms"],
                source_version=bundle["source_version"],
                crash_at=args.crash_at,
            )
        if args.json:
            dump_json(args.json, {"apply": result, "unified_view": bundle["unified_view"]})
        print(dumps({"ok": True, "apply": result}))
        return 0
    finally:
        conn.close()


def cmd_replace_threshold(args: argparse.Namespace) -> int:
    scenario, trace_bundle, metrics_config = _load_inputs(args)
    new_bundle = _threshold_bundle(
        v1=args.v1,
        v2_metadata=args.v2_metadata,
        origin_ms=scenario.origin_logical_ms,
        metric_names=set(metrics_config["metrics"]),
    )
    conn = connect(args.db)
    try:
        with file_lock(Path(args.db).parent / "locks", "threshold-replacement"):
            recover(conn, include_thresholds=True)
            old_payload = current_threshold(conn)
            old_view = old_payload.get("unified_view") if old_payload else None
            old_version = old_payload.get("threshold_version") if old_payload else None
            apply_result = apply_threshold_view(
                conn,
                unified_view=new_bundle["unified_view"],
                candidates_payload=new_bundle["candidates_payload"],
                threshold_version=new_bundle["threshold_version"],
                primary_snapshot_id=new_bundle["primary_snapshot_id"],
                generation=new_bundle["generation"],
                effective_logical_ms=new_bundle["effective_logical_ms"],
                source_version=new_bundle["source_version"],
                crash_at=args.crash_at,
            )
            plan = threshold_change_plan(
                conn=conn,
                scenario=scenario,
                metrics_config=metrics_config,
                old_view=old_view,
                new_view=new_bundle["unified_view"],
                old_threshold_version=old_version,
            )
            recompute_result = recompute_evaluations_for_new_threshold(
                conn=conn,
                scenario=scenario,
                records=trace_bundle.records,
                metrics_config=metrics_config,
                candidates=new_bundle["candidates"],
                new_threshold_version=new_bundle["threshold_version"],
                old_threshold_version=old_version,
                plan=plan,
            )
        output = {
            "ok": True,
            "apply": apply_result,
            "old_threshold_version": old_version,
            "new_threshold_version": new_bundle["threshold_version"],
            "invalidation_plan": plan,
            "recompute_result": recompute_result,
        }
        if args.json:
            dump_json(args.json, output)
        print(
            dumps(
                {
                    "ok": True,
                    "status": apply_result["status"],
                    "new_threshold_version": new_bundle["threshold_version"],
                    "recompute_count": plan["recompute_count"],
                    "reuse_evaluation_count": plan["reuse_evaluation_count"],
                    **recompute_result,
                    "json": args.json,
                }
            )
        )
        return 0
    finally:
        conn.close()


def cmd_plan_metric_change(args: argparse.Namespace) -> int:
    old_config = load_json(args.old_metrics)
    new_config = load_json(args.new_metrics)
    plan = metric_config_change_plan(old_config, new_config)
    if args.json:
        dump_json(args.json, plan)
    print(dumps(plan))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    scenario, trace_bundle, metrics_config = _load_inputs(args)
    conn = connect(args.db)
    try:
        thr_version = args.threshold_version
        if not thr_version:
            payload = current_threshold(conn)
            thr_version = payload["threshold_version"] if payload else "unversioned"
        report = full_verify(
            conn=conn,
            scenario=scenario,
            records=trace_bundle.records,
            metrics_config=metrics_config,
            threshold_version=thr_version,
        )
        report["threshold_version"] = thr_version
        if args.json:
            dump_json(args.json, report)
        summary = {
            "ok": report["all_within_error_budget"],
            "error_code": None if report["all_within_error_budget"] else ErrorCode.PRECISION_BUDGET,
            "checked": report["checked_metric_values"],
            "all_within_error_budget": report["all_within_error_budget"],
            "budget_violations": len(report["budget_violations"]),
            "sensitivity_pairs": report["sensitivity"]["pair_count"],
            "sensitive_pairs": report["sensitivity"]["sensitive_count"],
            "higher_scale_flips": report["sensitivity"]["higher_scale_flip_count"],
            "threshold_version": thr_version,
            "json": args.json,
        }
        print(dumps(summary))
        if not report["all_within_error_budget"]:
            first = report["budget_violations"][0]
            print(
                dumps(
                    {
                        "ok": False,
                        "error_code": ErrorCode.PRECISION_BUDGET,
                        "window": {
                            "scope_id": first["scope_id"],
                            "window_index": first["window_index"],
                            "metric": first["metric"],
                        },
                        "register_ids": first["register_ids"],
                    }
                ),
                file=sys.stderr,
            )
        return 0 if report["all_within_error_budget"] else 3
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capdiag", description="Capacity diagnostic engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="generate deterministic samples")
    p.add_argument("--root", default=".")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("validate", help="validate all delivered inputs")
    p.add_argument("--scenario", default="samples/scenario.json")
    p.add_argument("--traces", default="samples/traces.jsonl")
    p.add_argument("--metrics", default="samples/metrics.json")
    p.add_argument("--v1", dest="v1", default="samples/thresholds_v1.json")
    p.add_argument("--v2-metadata", default="samples/thresholds_v2_metadata.sqlite")
    p.add_argument("--json", default="out/validation.json")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="aggregate and commit one service")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--scenario", default="samples/scenario.json")
    p.add_argument("--traces", default="samples/traces.jsonl")
    p.add_argument("--metrics", default="samples/metrics.json")
    p.add_argument("--v1", default=None)
    p.add_argument("--v2-metadata", default=None)
    p.add_argument("--service", default=None)
    p.add_argument("--report", default=None)
    p.add_argument("--strict-traces", action="store_true")
    p.add_argument("--crash-at", choices=["conversion", "stage"], default=None)
    p.add_argument("--crash-window", type=int, default=2)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("recover", help="recover staged/interrupted state")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--json", default=None)
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("apply-threshold", help="atomically apply a threshold snapshot")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--scenario", default="samples/scenario.json")
    p.add_argument("--metrics", default="samples/metrics.json")
    p.add_argument("--v1", default="samples/thresholds_v1.json")
    p.add_argument("--v2-metadata", required=True)
    p.add_argument("--crash-at", choices=["threshold-stage"], default=None)
    p.add_argument("--json", default=None)
    p.set_defaults(func=cmd_apply_threshold)

    p = sub.add_parser("replace-threshold", help="apply v2 and recompute invalid evaluation closure")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--scenario", default="samples/scenario.json")
    p.add_argument("--traces", default="samples/traces.jsonl")
    p.add_argument("--metrics", default="samples/metrics.json")
    p.add_argument("--v1", default="samples/thresholds_v1.json")
    p.add_argument("--v2-metadata", default="samples/thresholds_v2_next_metadata.sqlite")
    p.add_argument("--json", default="out/threshold-replacement.json")
    p.add_argument("--crash-at", choices=["threshold-stage"], default=None)
    p.set_defaults(func=cmd_replace_threshold)

    p = sub.add_parser("plan-metric-change", help="compute dependency invalidation for config change")
    p.add_argument("--old-metrics", default="samples/metrics.json")
    p.add_argument("--new-metrics", default="samples/metrics_cpu_high_scale.json")
    p.add_argument("--json", default="out/metric-change-plan.json")
    p.set_defaults(func=cmd_plan_metric_change)

    p = sub.add_parser("verify", help="Decimal truth cross-check and sensitivity analysis")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--scenario", default="samples/scenario.json")
    p.add_argument("--traces", default="samples/traces.jsonl")
    p.add_argument("--metrics", default="samples/metrics.json")
    p.add_argument("--threshold-version", default=None)
    p.add_argument("--json", default="out/verification.json")
    p.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CapDiagError as exc:
        print(dumps(exc.to_dict()), file=sys.stderr)
        return 2 if exc.code != ErrorCode.PRECISION_BUDGET else 3
    except (sqlite3.Error, OSError, ValueError, KeyError) as exc:
        error = {"ok": False, "error_code": ErrorCode.SCHEMA, "error": str(exc)}
        print(dumps(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
