from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import json as json_module
from pathlib import Path

from lockdiag.engine import resolve_wait
from lockdiag.generator import generate_sample
from lockdiag.store import StateStore


ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, expect: int = 0) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        [sys.executable, "-m", "lockdiag", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != expect:
        raise AssertionError(
            f"command failed ({completed.returncode} != {expect}): {args}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.sample = cls.root / "sample"
        generate_sample(cls.sample)
        cls.state_a = cls.root / "run-a"
        cls.state_b = cls.root / "run-b"
        _run("run", "--sample", str(cls.sample), "--state", str(cls.state_a))
        _run("run", "--sample", str(cls.sample), "--state", str(cls.state_b))
        cls.report_a = cls.root / "report-a.json"
        cls.report_b = cls.root / "report-b.json"
        _run("report", "--state", str(cls.state_a), "--out", str(cls.report_a))
        _run("report", "--state", str(cls.state_b), "--out", str(cls.report_b))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_reports_are_byte_identical(self) -> None:
        left = self.report_a.read_bytes()
        right = self.report_b.read_bytes()
        self.assertEqual(left, right)
        self.assertEqual(hashlib.sha256(left).hexdigest(), hashlib.sha256(right).hexdigest())

    def test_report_contains_expected_diagnostics(self) -> None:
        report = json.loads(self.report_a.read_text(encoding="utf-8"))
        statuses = {tx["tx_id"]: tx["status"] for tx in report["transactions"]}
        self.assertEqual(
            statuses,
            {
                "T1": "COMMITTED",
                "T2": "ROLLED_BACK_DEADLOCK",
                "T3": "COMMITTED",
                "T4": "ABANDONED_TIMEOUT",
                "T5": "ROLLED_BACK_ACTIVE",
            },
        )
        self.assertEqual(len(report["deadlocks"]), 1)
        self.assertEqual(report["deadlocks"][0]["cycle"], ["T1", "T2", "T1"])
        self.assertEqual(report["deadlocks"][0]["victim_tx"], "T2")
        self.assertTrue(report["deadlocks"][0]["queue_revalidated"])
        self.assertEqual(len(report["wait_ledger"]), 4)
        self.assertGreater(len(report["precision_sensitive"]), 0)
        self.assertTrue(
            any(
                item["reason"] == "BUDGET_DECISION"
                for item in report["precision_sensitive"]
            )
        )
        paths = set(report["summary"]["index_paths"])
        self.assertIn("INDEX:idx_orders_customer", paths)
        self.assertIn("INDEX:idx_orders_status", paths)
        self.assertIn("INDEX:idx_customers_region", paths)
        self.assertTrue(
            any(
                row["index_path"] == "INDEX:idx_orders_customer"
                for row in report["index_attribution"]
            )
        )
        for wait in report["wait_ledger"]:
            self.assertEqual(
                wait["duration"]["raw_delta"],
                wait["release"]["raw_value"] - wait["enqueue"]["raw_value"],
            )
            self.assertIn("rounding_direction", wait["duration"])
            self.assertGreaterEqual(wait["duration"]["error_bound_micro_ms"], 0)
        self.assertEqual(report["final_locks"], [])
        self.assertEqual(
            [item for item in report["final_queue"] if item["status"] == "QUEUED"], []
        )

    def test_verify_and_multiprocess_probes(self) -> None:
        verified = _run("verify", "--state", str(self.state_a))
        payload = json.loads(verified.stdout)
        self.assertTrue(payload["ok"], payload)
        connection = sqlite3.connect(str(self.state_a / "state.db"))
        try:
            pids = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT worker_pid FROM operations WHERE worker_pid IS NOT NULL"
                )
            ]
            reentrant = connection.execute(
                "SELECT reason FROM lock_reentrancy WHERE tx_id='T1' AND op_seq=6"
            ).fetchone()
        finally:
            connection.close()
        self.assertGreaterEqual(len(pids), 2)
        self.assertIsNotNone(reentrant)
        self.assertEqual(reentrant[0], "UPGRADE_IN_PLACE")

    def test_crash_resume_is_account_identical_and_cleans_half_state(self) -> None:
        expected = self.report_a.read_bytes()
        for stage in ("enqueue", "grant", "abandon"):
            state = self.root / f"crash-{stage}"
            crashed = _run(
                "run",
                "--sample",
                str(self.sample),
                "--state",
                str(state),
                "--crash-at",
                stage,
                expect=97,
            )
            self.assertEqual(crashed.returncode, 97)
            connection = sqlite3.connect(str(state / "state.db"))
            try:
                connection.execute(
                    """
                    INSERT INTO recovery_intents(
                        tx_id, op_seq, stage, status, payload_json
                    ) VALUES ('GHOST', 1, ?, 'STAGED', '{}')
                    """,
                    (stage,),
                )
                connection.execute(
                    """
                    INSERT INTO lock_holders(
                        resource_key, resource_type, table_name, mode, tx_id,
                        op_seq, op_version, acquired_sample
                    ) VALUES ('["row","ghost",1]', 'row', 'ghost', 'exclusive',
                              'GHOST', 1, 1, 1)
                    """
                )
                connection.commit()
            finally:
                connection.close()
            _run("resume", "--state", str(state), "--sample", str(self.sample))
            report = self.root / f"report-crash-{stage}.json"
            _run("report", "--state", str(state), "--out", str(report))
            self.assertEqual(report.read_bytes(), expected, stage)
            verified = json.loads(_run("verify", "--state", str(state)).stdout)
            self.assertTrue(verified["ok"], verified)
            connection = sqlite3.connect(str(state / "state.db"))
            try:
                recovery = connection.execute(
                    "SELECT staged_removed, orphan_locks_removed FROM recovery_runs"
                ).fetchone()
            finally:
                connection.close()
            self.assertIsNotNone(recovery)
            self.assertGreaterEqual(recovery[0], 1)
            self.assertGreaterEqual(recovery[1], 1)

    def test_resume_is_idempotent(self) -> None:
        before = self.report_a.read_bytes()
        _run("resume", "--state", str(self.state_a), "--sample", str(self.sample))
        after_path = self.root / "after-resume.json"
        _run("report", "--state", str(self.state_a), "--out", str(after_path))
        self.assertEqual(after_path.read_bytes(), before)

    def test_resolving_same_wait_record_is_idempotent(self) -> None:
        with StateStore(self.state_a) as store:
            connection = store.open()
            wait = connection.execute(
                "SELECT * FROM waits WHERE status='RESOLVED' ORDER BY wait_id LIMIT 1"
            ).fetchone()
            before_accounts = [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM tx_accounts ORDER BY tx_id"
                )
            ]
            before_attribution = [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM attribution ORDER BY holder_tx, waiter_tx, index_path"
                )
            ]
            with store.transaction() as transaction:
                duplicate = resolve_wait(
                    transaction,
                    wait["wait_id"],
                    wait["resolve_sample"],
                    "GRANTED",
                    "GRANTED",
                    None,
                )
            self.assertIsNone(duplicate)
            after_accounts = [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM tx_accounts ORDER BY tx_id"
                )
            ]
            after_attribution = [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM attribution ORDER BY holder_tx, waiter_tx, index_path"
                )
            ]
            self.assertEqual(after_accounts, before_accounts)
            self.assertEqual(after_attribution, before_attribution)

    def test_status_command(self) -> None:
        payload = json.loads(_run("status", "--state", str(self.state_a)).stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["run"]["status"], "COMPLETE")
        self.assertEqual(len(payload["deadlocks"]), 1)
        self.assertEqual(len(payload["transactions"]), 5)

    def test_duplicate_run_rejected(self) -> None:
        completed = _run(
            "run",
            "--sample",
            str(self.sample),
            "--state",
            str(self.state_a),
            expect=2,
        )
        self.assertIn("E_STATE_EXISTS", completed.stdout)

    def test_scheduler_limit_marks_never_started_without_timeout(self) -> None:
        sample = self.root / "short-sample"
        shutil.copytree(self.sample, sample)
        config = json_module.loads((sample / "config.json").read_text(encoding="utf-8"))
        config["max_scheduler_steps"] = 3
        (sample / "config.json").write_text(
            json_module.dumps(config, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        state = self.root / "short-run"
        _run("run", "--sample", str(sample), "--state", str(state))
        report = json_module.loads(
            _run("report", "--state", str(state)).stdout
        )
        self.assertEqual(report["run_status"], "PAUSED")
        statuses = {tx["tx_id"]: tx["status"] for tx in report["transactions"]}
        self.assertIn("NOT_STARTED_CONCURRENCY", statuses.values())
        self.assertIn("RUNNING", statuses.values())
        self.assertNotIn("ABANDONED_TIMEOUT", statuses.values())


if __name__ == "__main__":
    unittest.main()
