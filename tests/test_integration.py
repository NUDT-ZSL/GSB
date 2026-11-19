from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lbdiag.engine import run_workbench
from lbdiag.errors import E_CLOCK_ROLLBACK, BusinessError
from lbdiag.storage import Store

from .helpers import ROOT, RunningServer, config_for_port, free_port, sample_raw, write_sample


class IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.refused_port = free_port()
        self.server = RunningServer(sample_raw()["test_service"]["routes"])
        self.server.__enter__()
        self.config = config_for_port(self.server.port, self.refused_port)
        self.sample_path = write_sample(
            self.directory / "sample.json", self.config.raw
        )

    def tearDown(self) -> None:
        self.server.__exit__(None, None, None)
        self.temp.cleanup()

    def cli(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        command = [sys.executable, "-m", "lbdiag", *args]
        merged = os.environ.copy()
        if env:
            merged.update(env)
        return subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=merged,
            check=False,
        )

    def test_full_run_idempotency_and_stable_output(self) -> None:
        db1 = self.directory / "one.db"
        db2 = self.directory / "two.db"
        args1 = [
            "run",
            "--sample",
            str(self.sample_path),
            "--db",
            str(db1),
            "--log-dir",
            str(self.directory / "logs1"),
        ]
        first = self.cli(*args1)
        second = self.cli(
            "run",
            "--sample",
            str(self.sample_path),
            "--db",
            str(db2),
            "--log-dir",
            str(self.directory / "logs2"),
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(first.stdout, second.stdout)

        repeated = self.cli(*args1)
        self.assertEqual(repeated.returncode, 0)
        self.assertEqual(first.stdout, repeated.stdout)

        status1 = self.cli(
            "status",
            "--sample",
            str(self.sample_path),
            "--db",
            str(db1),
            "--log-dir",
            str(self.directory / "logs3"),
        )
        status2 = self.cli(
            "status",
            "--sample",
            str(self.sample_path),
            "--db",
            str(db1),
            "--log-dir",
            str(self.directory / "logs4"),
        )
        self.assertEqual(status1.returncode, 0)
        self.assertEqual(status1.stdout, status2.stdout)
        status = json.loads(status1.stdout)["result"]
        self.assertEqual(status["summary"]["committed_window_count"], 21)
        self.assertEqual(status["summary"]["precision_sensitive_count"], 3)
        states = {item["instance_id"]: item for item in status["instances"]}
        self.assertEqual(states["alpha"]["state"], "HEALTHY")
        self.assertEqual(states["alpha"]["consecutive_successes"], 16)
        self.assertEqual(states["beta"]["effective_weight"]["raw_float"], 9.8)
        self.assertEqual(states["gamma"]["state"], "UNHEALTHY")
        self.assertEqual(states["delta"]["state"], "UNHEALTHY")
        self.assertEqual(states["epsilon"]["state"], "UNHEALTHY")
        self.assertEqual(states["zeta"]["state"], "UNHEALTHY")
        self.assertEqual(states["eta"]["state"], "UNHEALTHY")
        latest = {
            item["instance_id"]: item["latest_check"]["error_code"]
            for item in status["instances"]
        }
        self.assertEqual(latest["alpha"], "E_OK")
        self.assertEqual(latest["gamma"], "E_STATUS_MISMATCH")
        self.assertEqual(latest["delta"], "E_TIMEOUT")
        self.assertEqual(latest["epsilon"], "E_CONN_REFUSED")
        self.assertEqual(latest["zeta"], "E_MALFORMED_RESPONSE")
        self.assertEqual(latest["eta"], "E_SAMPLE_MISMATCH")

        explain = self.cli(
            "explain",
            "--sample",
            str(self.sample_path),
            "--db",
            str(db1),
            "--log-dir",
            str(self.directory / "logs5"),
            "--instance",
            "beta",
            "--window",
            "0",
        )
        self.assertEqual(explain.returncode, 0)
        record = json.loads(explain.stdout)["result"]["records"][0]
        self.assertTrue(record["precision_sensitive"])
        self.assertEqual(record["pair_id"], "alpha|beta|w0")
        self.assertIn("cumulative_error_upper_bound", record)
        self.assertEqual(record["error_source_chain"][-1]["step"], "verdict")

        verify = self.cli(
            "verify",
            "--sample",
            str(self.sample_path),
            "--db",
            str(db1),
            "--log-dir",
            str(self.directory / "logs6"),
        )
        self.assertEqual(verify.returncode, 0, verify.stdout)
        self.assertTrue(json.loads(verify.stdout)["result"]["ok"])

        store = Store(db1, create=False)
        try:
            before = store.snapshot(self.config)
            self.assertEqual(store.commit_windows("alpha", self.config), 0)
            checks = store.get_checks("alpha")
            self.assertFalse(store.insert_check(checks[0]))
            after = store.snapshot(self.config)
            self.assertEqual(before, after)
        finally:
            store.close()

    def test_crash_recovery_and_clock_rollback(self) -> None:
        db = self.directory / "crash.db"
        args = [
            "run",
            "--sample",
            str(self.sample_path),
            "--db",
            str(db),
            "--log-dir",
            str(self.directory / "logs"),
        ]
        crashed = self.cli(
            *args, env={"LBDIAG_TEST_CRASH_AFTER_PROBES": "12"}
        )
        self.assertNotEqual(crashed.returncode, 0)
        store = Store(db, create=False)
        try:
            checks = store.get_checks("alpha")
            self.assertEqual(len(checks), 12)
            self.assertEqual(store.committed_window_count("alpha"), 1)
            state = store.get_state("alpha")
            self.assertEqual(state["consecutive_successes"], 8)
            before_rollback = store.snapshot(self.config)
            self.assertFalse(
                before_rollback["instances"][0]["latest_check"]["window_committed"]
            )
        finally:
            store.close()

        pending_verify = self.cli(
            "verify",
            "--sample",
            str(self.sample_path),
            "--db",
            str(db),
            "--log-dir",
            str(self.directory / "logs"),
        )
        self.assertEqual(pending_verify.returncode, 0, pending_verify.stdout)
        pending_report = json.loads(pending_verify.stdout)["result"]
        alpha_check = next(
            item for item in pending_report["checks"] if item["name"] == "instance:alpha"
        )
        self.assertEqual(alpha_check["pending_complete_windows"], 1)

        resumed = self.cli(
            "resume",
            "--sample",
            str(self.sample_path),
            "--db",
            str(db),
            "--log-dir",
            str(self.directory / "logs"),
        )
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        store = Store(db, create=False)
        try:
            self.assertEqual(len(store.get_checks("alpha")), 16)
            self.assertEqual(store.committed_window_count("alpha"), 3)
            state = store.get_state("alpha")
            self.assertEqual(state["consecutive_successes"], 16)
            self.assertEqual(state["applied_check_count"], 16)
            before_rollback = store.snapshot(self.config)
            with self.assertRaises(BusinessError) as caught:
                run_workbench(store, self.config, now_ms=0)
            self.assertEqual(caught.exception.code, E_CLOCK_ROLLBACK)
            self.assertEqual(store.snapshot(self.config), before_rollback)
        finally:
            store.close()

        verify = self.cli(
            "verify",
            "--sample",
            str(self.sample_path),
            "--db",
            str(db),
            "--log-dir",
            str(self.directory / "logs"),
        )
        self.assertEqual(verify.returncode, 0, verify.stdout)

    def test_parallel_processes_different_instances(self) -> None:
        db = self.directory / "parallel.db"
        base = [
            "--sample",
            str(self.sample_path),
            "--db",
            str(db),
            "--log-dir",
            str(self.directory / "logs"),
        ]
        process_a = subprocess.Popen(
            [sys.executable, "-m", "lbdiag", "run", *base, "--instance", "alpha"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process_b = subprocess.Popen(
            [sys.executable, "-m", "lbdiag", "run", *base, "--instance", "beta"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out_a, err_a = process_a.communicate(timeout=30)
        out_b, err_b = process_b.communicate(timeout=30)
        self.assertEqual(process_a.returncode, 0, out_a + err_a)
        self.assertEqual(process_b.returncode, 0, out_b + err_b)

        store = Store(db, create=False)
        try:
            state_a = store.get_state("alpha")
            state_b = store.get_state("beta")
            self.assertEqual(state_a["consecutive_successes"], 16)
            self.assertEqual(state_b["consecutive_failures"], 1)
            self.assertEqual(store.committed_window_count("alpha"), 3)
            self.assertEqual(store.committed_window_count("beta"), 3)
            snapshot = store.snapshot(self.config)
            sensitive = [
                record
                for record in snapshot["sensitivity"]
                if record["precision_sensitive"]
            ]
            self.assertEqual(len(sensitive), 3)
        finally:
            store.close()

    def test_duplicate_window_commits_are_idempotent(self) -> None:
        db = self.directory / "duplicate.db"
        store = Store(db, create=True)
        try:
            store.initialize(self.config)
            from lbdiag.engine import run_workbench

            run_workbench(
                store,
                self.config,
                selected_instances=["alpha"],
                lock_dir=self.directory / "locks",
            )
            before = store.get_state("alpha")
            self.assertEqual(store.commit_windows("alpha", self.config), 0)
            self.assertEqual(store.commit_windows("alpha", self.config), 0)
            after = store.get_state("alpha")
            self.assertEqual(before, after)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()

