"""Command-line entry points for the multi-cluster ledger diagnostic console."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_declaration, load_inputs, load_sample_data
from .dashboard import Dashboard
from .demo import run_demo
from .diagnostics import run_anomaly
from .errors import E_LATENCY, E_SERVICE, BusinessError
from .ledger import Ledger
from .logging_utils import EventLogger
from .metrics import compute_latency_stats
from .scheduler import DispatchScheduler
from .service_manager import ClusterServiceManager
from .snapshot import load_snapshot, open_snapshot, recover_session, replay_snapshot, save_snapshot
from .ui import run_ui
from .verifier import AuthorityVerifier


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _common_load(args) -> tuple[Any, dict[str, Any]]:
    return load_inputs(args.config, args.sample_data)


def cmd_validate(args) -> int:
    declaration, samples = _common_load(args)
    _emit(
        {
            "ok": True,
            "code": None,
            "declaration_version": declaration.declaration_version,
            "declaration_fingerprint": declaration.fingerprint,
            "clusters": [
                {
                    "cluster_id": cluster.cluster_id,
                    "host": cluster.host,
                    "port": cluster.port,
                    "config_version": cluster.config_version,
                    "version_history": list(cluster.version_history),
                    "node_count": len(cluster.nodes),
                }
                for cluster in declaration.clusters
            ],
            "tasks": [
                {
                    "task_id": task.task_id,
                    "config_keys": list(task.config_keys),
                    "target_clusters": list(task.target_clusters),
                    "aggregation_mode": task.aggregation_mode,
                    "timeout_ms": task.timeout_ms,
                    "retries": task.retries,
                }
                for task in declaration.tasks
            ],
            "limits": {
                "global_concurrency": declaration.limits.global_concurrency,
                "per_cluster_concurrency": declaration.limits.per_cluster_concurrency,
                "query_budget_ms": declaration.limits.query_budget_ms,
            },
            "sample_schema_version": samples.get("schema_version"),
        }
    )
    return 0


def cmd_dispatch(args) -> int:
    declaration, _ = _common_load(args)
    logger = EventLogger(args.log)
    with Ledger(args.ledger) as ledger:
        reconciliation = ledger.reconcile(declaration)
        if args.no_start_services:
            schedule = DispatchScheduler(declaration, ledger, logger=logger).run(
                task_ids=args.task, cluster_ids=args.cluster
            )
        else:
            with ClusterServiceManager(args.config, args.sample_data, args.log) as manager:
                manager.start_many(declaration.clusters)
                schedule = DispatchScheduler(declaration, ledger, logger=logger).run(
                    task_ids=args.task, cluster_ids=args.cluster
                )
        payload = schedule.as_dict()
        payload.update(
            {
                "ok": schedule.failed == 0,
                "reconciliation": reconciliation,
                "ledger_revision": ledger.revision(),
            }
        )
        _emit(payload)
        return 0 if schedule.failed == 0 else 1


def cmd_query(args) -> int:
    declaration = load_declaration(args.config)
    logger = EventLogger(args.log)
    with Ledger(args.ledger) as ledger:
        reconciliation = ledger.reconcile(declaration)
        dashboard = Dashboard(
            declaration, ledger, logger, query_budget_ms=args.query_budget_ms
        )
        view = dashboard.query_view()
        view["reconciliation"] = reconciliation
        _emit(view)
        if args.fail_on_budget and view["latency_budget_exceeded"]:
            return 3
        return 0


def cmd_verify(args) -> int:
    declaration, samples = _common_load(args)
    logger = EventLogger(args.log)
    with Ledger(args.ledger) as ledger:
        reconciliation = ledger.reconcile(declaration)
        dashboard = Dashboard(
            declaration, ledger, logger, query_budget_ms=args.query_budget_ms
        )
        view = dashboard.query_view()
        report = AuthorityVerifier(declaration, samples, ledger, logger).verify(
            view, dashboard=dashboard
        )
        report["reconciliation"] = reconciliation
        report["view"] = {
            "data_version": view["data_version"],
            "cache_hit": view["cache_hit"],
            "latency_budget_exceeded": view["latency_budget_exceeded"],
        }
        _emit(report)
        return 0 if report["ok"] else 1


def cmd_stats(args) -> int:
    with Ledger(args.ledger) as ledger:
        interactions = ledger.fetch_interactions()
        current = [row for row in interactions if not row.get("is_stale", False)]
        payload = {
            "ok": True,
            "current": compute_latency_stats(current),
            "history": compute_latency_stats(interactions),
            "stale_interaction_count": len(interactions) - len(current),
            "caliber_version": ledger.latency_caliber_version(),
            "ledger_revision": ledger.revision(),
        }
        _emit(payload)
    return 0


def cmd_snapshot_save(args) -> int:
    declaration = load_declaration(args.config)
    logger = EventLogger(args.log)
    with Ledger(args.ledger) as ledger:
        reconciliation = ledger.reconcile(declaration)
        snap = save_snapshot(args.snapshot, declaration, ledger, logger)
        _emit(
            {
                "ok": True,
                "snapshot": args.snapshot,
                "declaration_version": snap["declaration_version"],
                "ledger_revision": snap["ledger_revision"],
                "completed_clusters": snap["completed_clusters"],
                "incomplete_units": snap["incomplete_units"],
                "reconciliation": reconciliation,
            }
        )
    return 0


def cmd_snapshot_open(args) -> int:
    logger = EventLogger(args.log)
    current = load_declaration(args.config) if args.config else None
    with Ledger(args.ledger) as ledger:
        report = open_snapshot(
            args.snapshot, ledger, current_declaration=current, logger=logger
        )
        _emit(report)
    return 0


def cmd_snapshot_replay(args) -> int:
    logger = EventLogger(args.log)
    samples = load_sample_data(args.sample_data)
    report = replay_snapshot(args.snapshot, samples, logger=logger)
    _emit(report)
    return 0 if report["ok"] else 1


def cmd_snapshot_recover(args) -> int:
    declaration = load_declaration(args.config)
    samples = load_sample_data(args.sample_data)
    logger = EventLogger(args.log)
    with Ledger(args.ledger) as ledger:
        report = recover_session(
            declaration, ledger, args.snapshot, logger, sample_data=samples
        )
        report["ledger_revision"] = ledger.revision()
        _emit(report)
    return 0 if report["ok"] else 1


def cmd_ui(args) -> int:
    logger = EventLogger(args.log)
    run_ui(
        config_path=args.config,
        sample_path=args.sample_data,
        ledger_path=args.ledger,
        logger=logger,
        snapshot_path=args.snapshot,
        query_budget_ms=args.query_budget_ms,
    )
    return 0


def cmd_demo(args) -> int:
    report, ok = run_demo(
        config_v1_path=args.config,
        config_v2_path=args.config_v2,
        sample_path=args.sample_data,
        workdir=args.workdir,
        latency_probe_budget_ms=args.latency_probe_budget_ms,
    )
    _emit(report)
    return 0 if ok else 1


def cmd_diagnose(args) -> int:
    logger = EventLogger(args.log)
    report = run_anomaly(
        args.anomaly,
        config_path=args.config,
        sample_path=args.sample_data,
        ledger_path=args.ledger,
        logger=logger,
    )
    _emit(report)
    return 0 if report.get("ok") else 2


def _add_data_args(parser: argparse.ArgumentParser, *, sample: bool = True) -> None:
    parser.add_argument("--config", required=True)
    if sample:
        parser.add_argument("--sample-data", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--log")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cluster_ledger",
        description="Local Windows multi-cluster configuration distribution ledger diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="strictly validate declaration and sample data")
    validate.add_argument("--config", required=True)
    validate.add_argument("--sample-data", required=True)
    validate.add_argument("--log")
    validate.set_defaults(func=cmd_validate)

    dispatch = subparsers.add_parser("dispatch", help="start services and dispatch all tasks")
    _add_data_args(dispatch)
    dispatch.add_argument("--task", action="append", help="limit to one or more task IDs")
    dispatch.add_argument("--cluster", action="append", help="limit to one or more cluster IDs")
    dispatch.add_argument("--no-start-services", action="store_true")
    dispatch.set_defaults(func=cmd_dispatch)

    query = subparsers.add_parser("query", help="fast cached dashboard snapshot")
    _add_data_args(query, sample=False)
    query.add_argument("--query-budget-ms", type=int)
    query.add_argument("--fail-on-budget", action="store_true")
    query.set_defaults(func=cmd_query)

    verify = subparsers.add_parser("verify", help="authoritative recomputation and view comparison")
    _add_data_args(verify)
    verify.add_argument("--query-budget-ms", type=int)
    verify.set_defaults(func=cmd_verify)

    stats = subparsers.add_parser("stats", help="latency statistics from the ledger")
    stats.add_argument("--ledger", required=True)
    stats.add_argument("--log")
    stats.set_defaults(func=cmd_stats)

    snapshot = subparsers.add_parser("snapshot", help="save, open, replay, or recover a session snapshot")
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_command", required=True)
    save = snapshot_sub.add_parser("save")
    _add_data_args(save)
    save.add_argument("--snapshot", required=True)
    save.set_defaults(func=cmd_snapshot_save)
    open_cmd = snapshot_sub.add_parser("open")
    open_cmd.add_argument("--snapshot", required=True)
    open_cmd.add_argument("--config")
    open_cmd.add_argument("--ledger", required=True)
    open_cmd.add_argument("--log")
    open_cmd.set_defaults(func=cmd_snapshot_open)
    replay = snapshot_sub.add_parser("replay")
    replay.add_argument("--snapshot", required=True)
    replay.add_argument("--sample-data", required=True)
    replay.add_argument("--log")
    replay.set_defaults(func=cmd_snapshot_replay)
    recover = snapshot_sub.add_parser("recover")
    _add_data_args(recover)
    recover.add_argument("--snapshot", required=True)
    recover.set_defaults(func=cmd_snapshot_recover)

    ui = subparsers.add_parser("ui", help="Tkinter diagnostic dashboard")
    _add_data_args(ui)
    ui.add_argument("--snapshot", default="var/session.json")
    ui.add_argument("--query-budget-ms", type=int)
    ui.set_defaults(func=cmd_ui)

    demo = subparsers.add_parser("demo", help="end-to-end v1 to v2 local demonstration")
    demo.add_argument("--config", default="data/clusters.json")
    demo.add_argument("--config-v2", default="data/clusters.v2.json")
    demo.add_argument("--sample-data", default="data/sample_data.json")
    demo.add_argument("--workdir", default="var/demo")
    demo.add_argument("--latency-probe-budget-ms", type=int, default=0)
    demo.add_argument("--log")
    demo.set_defaults(func=cmd_demo)

    diagnose = subparsers.add_parser("diagnose", help="inject port/version/ledger anomalies")
    diagnose.add_argument("--anomaly", choices=["port", "version_rollback", "ledger"], required=True)
    diagnose.add_argument("--config", default="data/clusters.json")
    diagnose.add_argument("--sample-data", default="data/sample_data.json")
    diagnose.add_argument("--ledger", default="var/demo/ledger.sqlite3")
    diagnose.add_argument("--log", default="var/demo/events.jsonl")
    diagnose.set_defaults(func=cmd_diagnose)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = EventLogger(getattr(args, "log", None))
    try:
        return int(args.func(args))
    except BusinessError as exc:
        logger.error(exc, event="cli_failure", command=args.command)
        print(
            json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )
        return 2
    except Exception as exc:
        error = BusinessError(
            E_SERVICE,
            f"unexpected CLI failure: {type(exc).__name__}: {exc}",
            details={"command": args.command},
        )
        logger.error(error, event="cli_failure", command=args.command)
        print(
            json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
