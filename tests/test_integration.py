from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cluster_ledger.client import DispatchClient
from cluster_ledger.config import load_inputs
from cluster_ledger.dashboard import Dashboard
from cluster_ledger.deterministic import compute_aggregate
from cluster_ledger.ledger import Ledger, result_checksum
from cluster_ledger.logging_utils import EventLogger
from cluster_ledger.scheduler import DispatchScheduler
from cluster_ledger.service_manager import ClusterServiceManager
from cluster_ledger.snapshot import (
    _snapshot_checksum,
    open_snapshot,
    recover_session,
    replay_snapshot,
    save_snapshot,
)
from cluster_ledger.verifier import AuthorityVerifier

from tests.helpers import DATA, ROOT, config_payload, free_ports, write_config


class LoopbackLedgerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.ports = free_ports(3)
        self.config_path = write_config(
            self.dir / "clusters.json", config_payload(ports=self.ports)
        )
        self.log_path = self.dir / "events.jsonl"
        self.logger = EventLogger(self.log_path)
        self.declaration, self.sample = load_inputs(
            self.config_path, DATA / "sample_data.json"
        )
        self.ledger = Ledger(self.dir / "ledger.sqlite3")
        self.ledger.reconcile(self.declaration)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def test_real_service_is_byte_stable(self) -> None:
        task = self.declaration.task_map()["runtime-rollout"]
        cluster = self.declaration.cluster_map()["edge-a"]
        with ClusterServiceManager(
            str(self.config_path), str(DATA / "sample_data.json"), str(self.log_path)
        ) as manager:
            manager.start_cluster(cluster)
            client = DispatchClient(retry_backoff_ms=0)
            first = client.dispatch(task, cluster)
            second = client.dispatch(task, cluster)
        self.assertEqual(first.response["summary"], second.response["summary"])
        self.assertEqual(first.response["components"], second.response["components"])
        self.assertEqual(first.response["config_version"], 1)
        self.assertIsInstance(first.response["calculation_duration_ms"], int)

    def test_dispatch_ledger_view_verify_and_latency_budget(self) -> None:
        with ClusterServiceManager(
            str(self.config_path), str(DATA / "sample_data.json"), str(self.log_path)
        ) as manager:
            manager.start_many(self.declaration.clusters)
            schedule = DispatchScheduler(
                self.declaration, self.ledger, logger=self.logger
            ).run()
        self.assertEqual(schedule.successful, 8)
        self.assertEqual(schedule.failed, 0)
        self.assertLessEqual(schedule.observed_peak_global, 5)
        self.assertLessEqual(schedule.observed_peak_by_cluster["edge-a"], 2)
        self.assertLessEqual(schedule.observed_peak_by_cluster["core-c"], 1)

        rows = self.ledger.fetch_results()
        self.assertEqual(len(rows), 8)
        for row in rows:
            task = self.declaration.task_map()[row["task_id"]]
            recomputed = compute_aggregate(
                cluster_id=row["cluster_id"],
                nodes=self.declaration.cluster_map()[row["cluster_id"]].nodes,
                config_version=row["config_version"],
                config_keys=task.config_keys,
                aggregation={"mode": task.aggregation_mode},
                sample_data=self.sample,
            )
            self.assertEqual(row["summary"], recomputed.summary)
            self.assertLessEqual(row["dispatch_started_mono_ms"], row["dispatch_finished_mono_ms"])

        dashboard = Dashboard(self.declaration, self.ledger, self.logger)
        first_view = dashboard.query_view()
        second_view = dashboard.query_view()
        self.assertFalse(first_view["cache_hit"])
        self.assertTrue(second_view["cache_hit"])
        self.assertEqual(len(first_view["results"]), 8)
        verify = AuthorityVerifier(
            self.declaration, self.sample, self.ledger, self.logger
        ).verify(second_view, dashboard=dashboard)
        self.assertTrue(verify["ok"], verify["errors"])

        tight = Dashboard(
            self.declaration, self.ledger, self.logger, query_budget_ms=0
        ).query_view()
        self.assertTrue(tight["latency_budget_exceeded"])
        self.assertEqual(tight["latency_event"]["code"], "E_LATENCY")
        self.assertIn("started_mono_ms", tight["latency_event"])
        self.assertIn("data_version", tight["latency_event"])

        current_rows = [row for row in self.ledger.fetch_interactions() if not row["is_stale"]]
        from cluster_ledger.metrics import compute_latency_stats
        stats = compute_latency_stats(current_rows)
        self.assertGreaterEqual(stats["overall"]["over_budget_count"], 1)
        self.assertTrue(stats["by_task_cluster"])
        self.assertEqual(stats["fixed_scale"], 1000)
        self.assertLessEqual(stats["quantization_error_upper_bound_ms"], 0.001)
        log_codes = [
            json.loads(line).get("code")
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("E_LATENCY", log_codes)

    def test_version_replacement_snapshot_replay_and_wholesale_restore(self) -> None:
        with ClusterServiceManager(
            str(self.config_path), str(DATA / "sample_data.json"), str(self.log_path)
        ) as manager:
            manager.start_many(self.declaration.clusters)
            DispatchScheduler(self.declaration, self.ledger, logger=self.logger).run()
        snapshot_v1 = self.dir / "session-v1.json"
        save_snapshot(snapshot_v1, self.declaration, self.ledger, self.logger)

        v2_ports = free_ports(3)
        v2_path = write_config(
            self.dir / "clusters-v2.json",
            config_payload(
                ports=v2_ports,
                versions={"edge-a": 2, "core-c": 2},
                declaration_version="test-v2",
            ),
        )
        v2, sample = load_inputs(v2_path, DATA / "sample_data.json")
        transition = self.ledger.reconcile(v2)
        changed = {
            item["cluster_id"]: (item["old_config_version"], item["new_config_version"])
            for item in transition["transitions"]
        }
        self.assertEqual(changed, {"edge-a": (1, 2), "core-c": (1, 2)})

        dashboard = Dashboard(v2, self.ledger, self.logger)
        before = dashboard.query_view()
        self.assertEqual(len(before["results"]), 3)
        self.assertEqual(
            [item["cluster_id"] for item in before["changed_clusters"]], ["core-c", "edge-a"]
        )
        self.assertTrue(all(
            row["config_version"] == v2.cluster_map()[row["cluster_id"]].config_version
            for row in before["results"]
        ))

        with ClusterServiceManager(
            str(v2_path), str(DATA / "sample_data.json"), str(self.log_path)
        ) as manager:
            manager.start_many(v2.clusters)
            schedule = DispatchScheduler(v2, self.ledger, logger=self.logger).run()
        self.assertEqual(schedule.successful, 8)
        after = dashboard.query_view()
        self.assertEqual(len(after["results"]), 8)
        self.assertTrue(after["cache_invalidation"]["cache_stale_marked"])
        self.assertEqual(after["cache_invalidation"]["replacement_mode"], "whole-view")
        self.assertTrue(all(
            row["config_version"] == v2.cluster_map()[row["cluster_id"]].config_version
            for row in after["results"]
        ))
        self.assertEqual(after["stale_result_count"], 5)
        verify = AuthorityVerifier(v2, sample, self.ledger, self.logger).verify(
            after, dashboard=dashboard
        )
        self.assertTrue(verify["ok"], verify["errors"])
        self.assertEqual(verify["checked_ledger_results"], 13)

        snapshot_v2 = self.dir / "session-v2.json"
        save_snapshot(snapshot_v2, v2, self.ledger, self.logger)
        replay = replay_snapshot(snapshot_v2, sample, logger=self.logger)
        self.assertTrue(replay["ok"], replay["errors"])

        tampered_path = self.dir / "session-tampered.json"
        snapshot_payload = json.loads(snapshot_v2.read_text(encoding="utf-8"))
        current_result = next(row for row in snapshot_payload["results"] if not row["is_stale"])
        current_result["summary"] = "sha256:tampered"
        current_result["ledger_checksum"] = result_checksum(current_result)
        snapshot_payload["snapshot_checksum"] = _snapshot_checksum(snapshot_payload)
        tampered_path.write_text(
            json.dumps(snapshot_payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        bad_replay = replay_snapshot(tampered_path, sample, logger=self.logger)
        self.assertFalse(bad_replay["ok"])
        self.assertEqual(bad_replay["errors"][0]["code"], "E_REPLAY")
        self.assertIn("task_id", bad_replay["errors"][0])
        self.assertIn("cluster_id", bad_replay["errors"][0])

        replacement = Ledger(self.dir / "replacement.sqlite3")
        try:
            opened = open_snapshot(
                snapshot_v1, replacement, current_declaration=v2, logger=self.logger
            )
            self.assertTrue(opened["declaration_mismatch"])
            self.assertEqual(opened["replacement_mode"], "wholesale")
            self.assertFalse(opened["merged"])
            self.assertEqual(len(replacement.fetch_results()), 8)
            self.assertTrue(all(row["config_version"] == 1 for row in replacement.fetch_results()))
            v1_replay = replay_snapshot(snapshot_v1, sample, logger=self.logger)
            self.assertTrue(v1_replay["ok"], v1_replay["errors"])
        finally:
            replacement.close()

    def test_forced_kill_restores_only_committed_rows_and_marks_unknown(self) -> None:
        snapshot = self.dir / "before-kill.json"
        save_snapshot(snapshot, self.declaration, self.ledger, self.logger)
        db_path = self.dir / "child.sqlite3"
        child_code = r'''
import sys, time
from cluster_ledger.config import load_declaration
from cluster_ledger.ledger import Ledger
from cluster_ledger.scheduler import DispatchScheduler
config, db = sys.argv[1], sys.argv[2]
declaration = load_declaration(config)
ledger = Ledger(db)
ledger.reconcile(declaration)
DispatchScheduler(declaration, ledger).run(task_ids={"runtime-rollout"}, cluster_ids={"edge-a"})
print("COMMITTED", flush=True)
time.sleep(30)
'''
        with ClusterServiceManager(
            str(self.config_path), str(DATA / "sample_data.json"), str(self.log_path)
        ) as manager:
            manager.start_many(self.declaration.clusters)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, str(self.config_path), str(db_path)],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 10
                committed = 0
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        stderr = child.stderr.read().decode("utf-8", errors="replace")
                        self.fail(f"forced-kill worker exited early: {stderr}")
                    try:
                        probe = Ledger(db_path)
                        try:
                            committed = len(probe.fetch_results())
                        finally:
                            probe.close()
                    except Exception:
                        committed = 0
                    if committed == 1:
                        break
                    time.sleep(0.05)
                self.assertEqual(committed, 1)
                child.kill()
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()
        recovery_ledger = Ledger(db_path)
        try:
            report = recover_session(
                self.declaration, recovery_ledger, snapshot, self.logger
            )
            self.assertFalse(report["clean_shutdown"])
            self.assertIsNone(report["exit_report"])
            self.assertEqual(report["marked_unknown_count"], 7)
            self.assertEqual(len(recovery_ledger.fetch_results()), 1)
            unknown = [row for row in recovery_ledger.read_consistent_state()["recovery"] if row["status"] == "unknown"]
            self.assertEqual(len(unknown), 7)
        finally:
            recovery_ledger.close()


if __name__ == "__main__":
    unittest.main()
