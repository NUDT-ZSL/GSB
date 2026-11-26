from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import load_and_run, run_cli


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def test_crash_after_process_commit_resumes_to_identical_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clean_db = Path(temporary) / "clean.db"
            load_and_run(clean_db)
            clean_status, _ = run_cli(["status", "--db", str(clean_db)])
            clean_payload = json.loads(clean_status.stdout)

            crash_db = Path(temporary) / "crash.db"
            loaded, _ = run_cli(
                ["load", "--data-dir", "sample", "--db", str(crash_db)]
            )
            self.assertEqual(loaded.returncode, 0)
            crashed, _ = run_cli(
                ["run", "--data-dir", "sample", "--db", str(crash_db)],
                env={"QUOTA_LEDGER_CRASH_AT": "process:3"},
                check_json=False,
            )
            self.assertEqual(crashed.returncode, 75)
            status_completed, crashed_status = run_cli(
                ["status", "--db", str(crash_db)]
            )
            self.assertEqual(status_completed.returncode, 0)
            self.assertEqual(crashed_status["process_round_watermark"], 3)
            self.assertEqual(crashed_status["adjust_round_watermark"], 2)

            resumed, _ = run_cli(
                ["resume", "--data-dir", "sample", "--db", str(crash_db)]
            )
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            verified, verify_payload = run_cli(["verify", "--db", str(crash_db)])
            self.assertEqual(verified.returncode, 0)
            self.assertTrue(verify_payload["ok"])
            self.assertEqual(
                verify_payload["state_digest"],
                clean_payload["state_digest"],
            )

            connection = sqlite3.connect(crash_db)
            try:
                duplicate_entries = connection.execute(
                    "SELECT COUNT(*) FROM ("
                    "SELECT event_key, COUNT(*) AS c FROM ledger_entries GROUP BY event_key "
                    "HAVING c > 1)"
                ).fetchone()[0]
                duplicate_policies = connection.execute(
                    "SELECT COUNT(*) FROM ("
                    "SELECT policy_id, COUNT(*) AS c FROM policy_events GROUP BY policy_id, tenant "
                    "HAVING c > 1)"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(duplicate_entries, 0)
            self.assertEqual(duplicate_policies, 0)

    def test_two_parallel_runs_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "concurrent.db"
            loaded, _ = run_cli(
                ["load", "--data-dir", "sample", "--db", str(db)]
            )
            self.assertEqual(loaded.returncode, 0)
            command = [
                sys.executable,
                "-m",
                "quota_ledger",
                "run",
                "--data-dir",
                "sample",
                "--db",
                str(db),
            ]
            first = subprocess.Popen(
                command,
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=os.environ.copy(),
            )
            second = subprocess.Popen(
                command,
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=os.environ.copy(),
            )
            first_stdout, first_stderr = first.communicate(timeout=30)
            second_stdout, second_stderr = second.communicate(timeout=30)
            self.assertEqual(first.returncode, 0, first_stdout + first_stderr)
            self.assertEqual(second.returncode, 0, second_stdout + second_stderr)
            verified, verify_payload = run_cli(["verify", "--db", str(db)])
            self.assertEqual(verified.returncode, 0)
            self.assertTrue(verify_payload["ok"])

            connection = sqlite3.connect(db)
            try:
                accepted = connection.execute(
                    "SELECT COUNT(*) FROM applications WHERE state IN ('accepted','released')"
                ).fetchone()[0]
                entries = connection.execute(
                    "SELECT COUNT(DISTINCT event_key) FROM ledger_entries"
                ).fetchone()[0]
                total_entries = connection.execute(
                    "SELECT COUNT(*) FROM ledger_entries"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(accepted, 9)
            self.assertEqual(entries, total_entries)

    def test_repeated_load_and_run_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "idempotent.db"
            load_and_run(db)
            status_before, before = run_cli(["status", "--db", str(db)])
            self.assertEqual(status_before.returncode, 0)
            digest_before = before["state_digest"]
            load_again, load_payload = run_cli(
                ["load", "--data-dir", "sample", "--db", str(db)]
            )
            self.assertEqual(load_again.returncode, 0)
            self.assertTrue(load_payload["duplicate_load"])
            self.assertFalse(load_payload["applied"])
            self.assertTrue(
                all(row["recomputed"] == 0 for row in load_payload["merge"])
            )
            run_again, _ = run_cli(
                ["run", "--data-dir", "sample", "--db", str(db)],
                check_json=False,
            )
            self.assertEqual(run_again.returncode, 0)
            status_after, after = run_cli(["status", "--db", str(db)])
            self.assertEqual(status_after.returncode, 0)
            self.assertEqual(after["state_digest"], digest_before)

