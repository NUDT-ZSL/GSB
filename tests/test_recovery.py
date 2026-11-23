from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from walcp.engine import AppService

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
NOW = 1_700_000_500_000


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, *args: str, env_extra: dict[str, str] | None = None):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-m", "walcp", *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )

    def _load_apply(self, db: Path) -> None:
        self._load(db)
        applied = self._run("apply", "--db", str(db), "--now-ms", str(NOW))
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)

    def _load(self, db: Path) -> None:
        loaded = self._run(
            "load",
            "--db",
            str(db),
            "--samples",
            str(SAMPLES),
            "--now-ms",
            str(NOW),
        )
        self.assertEqual(loaded.returncode, 0, loaded.stdout + loaded.stderr)

    def test_forced_termination_during_apply_then_resume(self) -> None:
        db = self.work / "state.db"
        self._load(db)
        crashed = self._run(
            "apply",
            "--db",
            str(db),
            "--now-ms",
            str(NOW),
            env_extra={
                "WALCP_FAULT_POINT": "apply_after_rows",
                "WALCP_FAULT_TXN": "18",
            },
        )
        self.assertEqual(crashed.returncode, 75)
        status = AppService(db).status()
        self.assertEqual(status["watermark"], 17)
        self.assertEqual(status["pending_transactions"], [18])
        self.assertEqual(len(status["pending_operations"]), 1)
        resumed = AppService(db).resume(now_ms=NOW)
        self.assertTrue(resumed["ok"])
        self.assertTrue(resumed["recovery"])
        self.assertEqual(resumed["watermark"], 22)
        final = AppService(db).status()
        self.assertEqual(final["pending_transactions"], [])
        self.assertEqual(final["delay_summary"]["count"], 8)
        self.assertEqual(
            [record["txn_id"] for record in final["delay_summary"]["records"]],
            list(range(15, 23)),
        )
        self.assertTrue(AppService(db).verify()["matched"])

    def test_forced_termination_during_edit_rolls_back(self) -> None:
        db = self.work / "state.db"
        self._load_apply(db)
        before = AppService(db).status()
        crashed = self._run(
            "edit",
            "--db",
            str(db),
            "--txn-id",
            "18",
            "--set",
            'after={"balance":99}',
            "--now-ms",
            str(NOW + 1000),
            env_extra={"WALCP_FAULT_POINT": "edit_before_commit"},
        )
        self.assertEqual(crashed.returncode, 75)
        after_crash = AppService(db).status()
        self.assertFalse(after_crash["needs_rebuild"])
        self.assertEqual(after_crash["edit_count"], before["edit_count"])
        self.assertEqual(len(after_crash["pending_operations"]), 1)
        resumed = AppService(db).resume(now_ms=NOW + 1000)
        decisions = [item["decision"] for item in resumed["recovery"]]
        self.assertIn("aborted_no_partial_state", decisions)
        self.assertTrue(AppService(db).verify()["matched"])

    def test_forced_termination_during_checkpoint_keeps_pointer(self) -> None:
        db = self.work / "state.db"
        self._load_apply(db)
        package = self.work / "gen3"
        crashed = self._run(
            "checkpoint",
            "create",
            "--db",
            str(db),
            "--out",
            str(package),
            "--now-ms",
            str(NOW + 200_000),
            env_extra={"WALCP_FAULT_POINT": "checkpoint_before_commit"},
        )
        self.assertEqual(crashed.returncode, 75)
        status = AppService(db).status()
        self.assertEqual(status["checkpoint_generation"], 2)
        self.assertEqual(len(status["pending_operations"]), 1)
        resumed = AppService(db).resume(now_ms=NOW + 200_000)
        self.assertIn(
            "aborted_no_partial_state",
            [item["decision"] for item in resumed["recovery"]],
        )
        created = AppService(db).checkpoint(
            out_dir=package, now_ms=NOW + 200_000
        )
        self.assertEqual(created["decision"], "created")
        self.assertEqual(created["generation"], 3)
        self.assertTrue(AppService(db).verify()["matched"])

    def test_crash_recovery_matches_uninterrupted_rows(self) -> None:
        crashed_db = self.work / "crashed.db"
        control_db = self.work / "control.db"
        self._load(crashed_db)
        self._load_apply(control_db)
        crashed = self._run(
            "apply",
            "--db",
            str(crashed_db),
            "--now-ms",
            str(NOW),
            env_extra={
                "WALCP_FAULT_POINT": "apply_after_rows",
                "WALCP_FAULT_TXN": "18",
            },
        )
        self.assertEqual(crashed.returncode, 75)
        AppService(crashed_db).resume(now_ms=NOW)
        crashed_rows = AppService(crashed_db)._store()
        control_rows = AppService(control_db)._store()
        with crashed_rows as left, control_rows as right:
            left.connect(create=False)
            right.connect(create=False)
            left_data = [
                tuple(row)
                for row in left.db.execute(
                    "SELECT table_name,row_key,version,txn_id,deleted,value_json "
                    "FROM _rows ORDER BY table_name,row_key"
                )
            ]
            right_data = [
                tuple(row)
                for row in right.db.execute(
                    "SELECT table_name,row_key,version,txn_id,deleted,value_json "
                    "FROM _rows ORDER BY table_name,row_key"
                )
            ]
        self.assertEqual(left_data, right_data)


if __name__ == "__main__":
    unittest.main()
