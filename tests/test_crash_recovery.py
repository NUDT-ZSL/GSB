from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from .helpers import cli_service, parse_stderr, parse_stdout, run_cli


class CrashRecoveryTests(unittest.TestCase):
    def _load_cli(self, tmp: Path) -> None:
        result = run_cli(
            "load",
            "--db",
            str(tmp / "db.sqlite"),
            "--lock",
            str(tmp / "db.lock"),
            "--tmp",
            str(tmp / "tmp"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def _dispatch_cli(self, tmp: Path, *extra: str) -> object:
        return run_cli(
            "dispatch",
            "--db",
            str(tmp / "db.sqlite"),
            "--lock",
            str(tmp / "db.lock"),
            "--tmp",
            str(tmp / "tmp"),
            *extra,
        )

    def test_prepare_phase_crash_recovers_without_quota_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            self._load_cli(tmp)
            crashed = self._dispatch_cli(
                tmp, "--crash-at", "after_prepare_windows"
            )
            self.assertEqual(crashed.returncode, 137, crashed.stderr)
            service = cli_service(tmp)
            before = service.status(details=False)
            self.assertGreater(before["prepared_windows_total"], 0)
            self.assertEqual(before["committed_batches_total"], 0)
            self.assertTrue(all(row["compute_in_use"] == 0.0 for row in before["replicas"]))
            self.assertTrue(service.verify()["ok"])

            resumed = run_cli(
                "resume",
                "--db",
                str(tmp / "db.sqlite"),
                "--lock",
                str(tmp / "db.lock"),
                "--tmp",
                str(tmp / "tmp"),
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            verify = service.verify()
            self.assertTrue(verify["ok"], verify)
            self.assertEqual(verify["stats"]["request_assignments"], 18)
            self.assertEqual(verify["stats"]["committed_windows"], 10)
            self.assertEqual(verify["stats"]["batches"], 14)
            self.assertTrue(
                all(
                    row["compute_in_use"] == 0.0
                    and row["memory_in_use_mib"] == 0.0
                    for row in service.status()["replicas"]
                )
            )

    def test_in_transaction_crash_rolls_back_and_resume_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            self._load_cli(tmp)
            crashed = self._dispatch_cli(
                tmp, "--crash-at", "inside_window_commit"
            )
            self.assertEqual(crashed.returncode, 137)
            service = cli_service(tmp)
            before = service.status()
            self.assertGreater(before["prepared_windows_total"], 0)
            self.assertEqual(before["committed_batches_total"], 0)
            self.assertTrue(service.verify()["ok"])
            resumed = run_cli(
                "resume",
                "--db",
                str(tmp / "db.sqlite"),
                "--lock",
                str(tmp / "db.lock"),
                "--tmp",
                str(tmp / "tmp"),
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            verify = service.verify()
            self.assertTrue(verify["ok"], verify)
            self.assertEqual(verify["stats"]["request_assignments"], 18)

    def test_after_commit_crash_keeps_active_quota_then_settles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            self._load_cli(tmp)
            crashed = self._dispatch_cli(
                tmp, "--crash-at", "after_commit_before_settle"
            )
            self.assertEqual(crashed.returncode, 137)
            service = cli_service(tmp)
            before = service.status()
            self.assertEqual(before["prepared_windows_total"], 0)
            self.assertEqual(before["committed_batches_total"], 14)
            self.assertTrue(
                any(row["compute_in_use"] > 0 for row in before["version_states"])
            )
            self.assertTrue(service.verify()["ok"])
            failed = service.record_failure(
                "req-001", attempts=3, error="SIMULATED_EXECUTOR_CRASH"
            )
            self.assertEqual(failed["failure_count"], 3)
            resumed = run_cli(
                "resume",
                "--db",
                str(tmp / "db.sqlite"),
                "--lock",
                str(tmp / "db.lock"),
                "--tmp",
                str(tmp / "tmp"),
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            verify = service.verify()
            self.assertTrue(verify["ok"], verify)
            self.assertEqual(verify["stats"]["request_assignments"], 18)
            conn = sqlite3.connect(service.db_path)
            conn.row_factory = sqlite3.Row
            try:
                failed_request = conn.execute(
                    "SELECT * FROM requests WHERE request_id='req-001'"
                ).fetchone()
                self.assertEqual(failed_request["status"], "expired")
                self.assertEqual(failed_request["outcome"], "EXECUTION_FAILED")
                self.assertEqual(failed_request["failure_count"], 3)
                deadline_request = conn.execute(
                    "SELECT * FROM requests WHERE request_id='req-007'"
                ).fetchone()
                self.assertEqual(deadline_request["status"], "expired")
                self.assertEqual(deadline_request["outcome"], "DEADLINE_MISSED")
                usage = conn.execute(
                    "SELECT COUNT(*) AS n FROM batches WHERE usage_released=0"
                ).fetchone()
                self.assertEqual(usage["n"], 0)
            finally:
                conn.close()
            self.assertTrue(
                all(
                    row["compute_in_use"] == 0.0
                    and row["memory_in_use_mib"] == 0.0
                    for row in service.status()["version_states"]
                )
            )

    def test_inflight_windows_stay_bound_to_old_replica_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            self._load_cli(tmp)
            crashed = self._dispatch_cli(
                tmp, "--crash-at", "after_prepare_windows"
            )
            self.assertEqual(crashed.returncode, 137)
            upgraded = run_cli(
                "load",
                "--db",
                str(tmp / "db.sqlite"),
                "--lock",
                str(tmp / "db.lock"),
                "--tmp",
                str(tmp / "tmp"),
                "--replicas",
                "samples/replicas-v2.json",
            )
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            upgrade_json = parse_stdout(upgraded)
            self.assertEqual(upgrade_json["change_kind"], "incremental_append")
            self.assertEqual(upgrade_json["active_replica_version"], 2)
            resumed = run_cli(
                "resume",
                "--db",
                str(tmp / "db.sqlite"),
                "--lock",
                str(tmp / "db.lock"),
                "--tmp",
                str(tmp / "tmp"),
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            service = cli_service(tmp)
            verify = service.verify()
            self.assertTrue(verify["ok"], verify)
            details = service.status(details=True)
            self.assertEqual(details["active_replica_version"], 2)
            self.assertTrue(details["windows"])
            self.assertEqual(
                {window["replica_version"] for window in details["windows"]},
                {1},
            )
            states = {
                row["replica_version"]: row
                for row in details["version_states"]
            }
            self.assertGreater(states[1]["committed_windows"], 0)
            self.assertEqual(states[2]["committed_windows"], 0)
            rollback = run_cli(
                "load",
                "--db",
                str(tmp / "db.sqlite"),
                "--lock",
                str(tmp / "db.lock"),
                "--tmp",
                str(tmp / "tmp"),
                "--replicas",
                "samples/replicas.json",
            )
            self.assertEqual(rollback.returncode, 2)
            self.assertEqual(
                parse_stderr(rollback)["error"]["code"],
                "E_VERSION_ROLLBACK",
            )


if __name__ == "__main__":
    unittest.main()

