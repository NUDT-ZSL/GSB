"""End-to-end local demonstration with real loopback subprocesses."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import load_inputs
from .dashboard import Dashboard
from .ledger import Ledger
from .logging_utils import EventLogger
from .scheduler import DispatchScheduler
from .service_manager import ClusterServiceManager
from .snapshot import open_snapshot, recover_session, replay_snapshot, save_snapshot
from .verifier import AuthorityVerifier


def _reset_demo_files(workdir: Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    for name in (
        "ledger.sqlite3", "ledger.sqlite3-wal", "ledger.sqlite3-shm",
        "events.jsonl", "session-v1.json", "session-final.json", "report.json",
        "partial.sqlite3", "partial.sqlite3-wal", "partial.sqlite3-shm",
        "snapshot-replacement.sqlite3", "snapshot-replacement.sqlite3-wal",
        "snapshot-replacement.sqlite3-shm",
    ):
        (workdir / name).unlink(missing_ok=True)


def run_demo(*, config_v1_path: str, config_v2_path: str, sample_path: str,
             workdir: str, latency_probe_budget_ms: int = 0) -> tuple[dict[str, Any], bool]:
    root = Path(workdir)
    _reset_demo_files(root)
    log_path = root / "events.jsonl"
    ledger_path = root / "ledger.sqlite3"
    snapshot_v1 = root / "session-v1.json"
    snapshot_final = root / "session-final.json"
    report_path = root / "report.json"
    logger = EventLogger(log_path)
    ledger = Ledger(ledger_path)
    report: dict[str, Any] = {"ok": False, "phase": "startup", "steps": []}
    try:
        declaration_v1, sample_data = load_inputs(config_v1_path, sample_path)
        declaration_v2, _ = load_inputs(config_v2_path, sample_path)
        ledger.reconcile(declaration_v1)

        with ClusterServiceManager(config_v1_path, sample_path, str(log_path)) as manager:
            manager.start_many(declaration_v1.clusters)
            phase1_schedule = DispatchScheduler(
                declaration_v1, ledger, logger=logger
            ).run()
        dashboard_v1 = Dashboard(declaration_v1, ledger, logger)
        phase1_view = dashboard_v1.query_view()
        phase1_cache_view = dashboard_v1.query_view()
        budget_probe = Dashboard(
            declaration_v1, ledger, logger, query_budget_ms=latency_probe_budget_ms
        ).query_view()
        phase1_verify = AuthorityVerifier(
            declaration_v1, sample_data, ledger, logger
        ).verify(phase1_cache_view, dashboard=dashboard_v1)
        save_snapshot(snapshot_v1, declaration_v1, ledger, logger)
        report["steps"].append(
            {
                "step": "v1-dispatch-and-verify",
                "schedule": phase1_schedule.as_dict(),
                "view": {
                    "data_version": phase1_view["data_version"],
                    "result_count": len(phase1_view["results"]),
                    "first_query_cache_hit": phase1_view["cache_hit"],
                    "second_query_cache_hit": phase1_cache_view["cache_hit"],
                },
                "latency_budget_probe": {
                    "budget_ms": latency_probe_budget_ms,
                    "exceeded": budget_probe["latency_budget_exceeded"],
                    "query_latency": budget_probe["query_latency"],
                    "event": budget_probe["latency_event"],
                },
                "verify": phase1_verify,
            }
        )

        transition = ledger.reconcile(declaration_v2)
        dashboard_v2 = Dashboard(declaration_v2, ledger, logger)
        before_view = dashboard_v2.query_view()
        with ClusterServiceManager(config_v2_path, sample_path, str(log_path)) as manager:
            manager.start_many(declaration_v2.clusters)
            phase2_schedule = DispatchScheduler(
                declaration_v2, ledger, logger=logger
            ).run()
            # A separate ledger receives exactly one committed result. This is
            # the state left by an abrupt process stop, used for recovery proof.
            partial_ledger = Ledger(root / "partial.sqlite3")
            try:
                partial_ledger.reconcile(declaration_v2)
                DispatchScheduler(declaration_v2, partial_ledger, logger=logger).run(
                    task_ids={"runtime-rollout"}, cluster_ids={"edge-a"}
                )
            finally:
                partial_ledger.close()
        after_view = dashboard_v2.query_view()
        after_cache_view = dashboard_v2.query_view()
        phase2_verify = AuthorityVerifier(
            declaration_v2, sample_data, ledger, logger
        ).verify(after_cache_view, dashboard=dashboard_v2)
        save_snapshot(snapshot_final, declaration_v2, ledger, logger)
        replay = replay_snapshot(snapshot_final, sample_data, logger=logger)
        recovery_ledger = Ledger(root / "partial.sqlite3")
        try:
            recovery = recover_session(
                declaration_v2, recovery_ledger, snapshot_final, logger
            )
        finally:
            recovery_ledger.close()
        report["steps"].append(
            {
                "step": "v2-transition-dispatch-and-verify",
                "transition": transition,
                "before_dispatch": {
                    "data_version": before_view["data_version"],
                    "result_count": len(before_view["results"]),
                    "stale_result_count": before_view["stale_result_count"],
                    "changed_clusters": before_view["changed_clusters"],
                    "tasks": [
                        {
                            "task_id": task["task_id"],
                            "completed_units": task["completed_units"],
                            "total_units": task["total_units"],
                        }
                        for task in before_view["tasks"]
                    ],
                },
                "schedule": phase2_schedule.as_dict(),
                "after_dispatch": {
                    "data_version": after_view["data_version"],
                    "result_count": len(after_view["results"]),
                    "cache_hit_second_query": after_cache_view["cache_hit"],
                    "changed_clusters": after_view["changed_clusters"],
                    "cluster_versions": {
                        cluster["cluster_id"]: cluster["config_version"]
                        for cluster in after_view["clusters"]
                    },
                },
                "verify": phase2_verify,
                "replay": replay,
                "partial_recovery": recovery,
            }
        )

        replacement_db = root / "snapshot-replacement.sqlite3"
        replacement_ledger = Ledger(replacement_db)
        try:
            replacement = open_snapshot(
                snapshot_v1, replacement_ledger,
                current_declaration=declaration_v2, logger=logger,
            )
            v1_replay = replay_snapshot(snapshot_v1, sample_data, logger=logger)
            replacement["replay_v1"] = v1_replay
        finally:
            replacement_ledger.close()
        report["steps"].append(
            {"step": "snapshot-declaration-mismatch-wholesale-replacement", "result": replacement}
        )
        report["latency_stats_current"] = dashboard_v2.latency_stats()
        report["ok"] = bool(
            phase1_schedule.failed == 0
            and phase2_schedule.failed == 0
            and phase1_verify["ok"]
            and phase2_verify["ok"]
            and replay["ok"]
            and v1_replay["ok"]
            and replacement["declaration_mismatch"]
            and replacement["replacement_mode"] == "wholesale"
            and recovery["marked_unknown_count"] > 0
            and budget_probe["latency_budget_exceeded"]
        )
        report["phase"] = "complete"
        report["paths"] = {
            "ledger": str(ledger_path),
            "session_v1": str(snapshot_v1),
            "session_final": str(snapshot_final),
            "structured_log": str(log_path),
            "report": str(report_path),
        }
        report["clock_basis"] = {
            "client": "time.perf_counter_ns, per-process monotonic origin",
            "server": "time.perf_counter_ns, independent per-service-process origin",
            "truth": "local deterministic recomputation by config version",
        }
        with report_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        logger.emit("demo_completed", ok=report["ok"], report_path=str(report_path))
        return report, bool(report["ok"])
    finally:
        ledger.close()
