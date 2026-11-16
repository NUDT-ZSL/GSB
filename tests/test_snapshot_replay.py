from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from txlab.models import SchedulerConfig
from txlab.replay import replay_snapshot
from txlab.service import execute_script_file
from txlab.snapshot import restore_snapshot, save_snapshot
from txlab.storage import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]
INIT_SQL = ROOT / "samples" / "initial_rows.sql"


class SnapshotReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="txlab-snapshot-test-")
        self.temp_dir = Path(self.temp.name)
        self.snapshot_dir = self.temp_dir / "snapshots"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, sample: str, db_name: str, budget: int = 500) -> dict:
        return execute_script_file(
            ROOT / "samples" / sample,
            db_path=self.temp_dir / db_name,
            init_sql_path=INIT_SQL,
            config=SchedulerConfig(
                lock_wait_budget_ms=budget,
                max_concurrency=2,
                tick_ms=10,
            ),
        )

    def test_snapshot_restore_is_wholesale_replacement(self) -> None:
        normal = self._run("normal_parallel.json", "workspace.sqlite3")
        snapshot_path = save_snapshot(
            self.snapshot_dir,
            "normal-v3",
            normal,
            initial_sql=INIT_SQL.read_text(encoding="utf-8"),
        )

        # Change the workspace to a different generation/version.
        timeout = self._run("lock_timeout.json", "workspace.sqlite3", budget=40)
        self.assertEqual(timeout["final_data_version"], 1)

        with SQLiteStore(self.temp_dir / "workspace.sqlite3") as store:
            report = restore_snapshot(snapshot_path, store)
            self.assertEqual(report["restore_mode"], "full_replace")
            self.assertTrue(report["version_mismatch"])
            self.assertEqual(report["current_session_version"], 1)
            self.assertEqual(report["snapshot_session_version"], 3)
            self.assertEqual(store.data_version(), 3)
            restored_a = store.get_row("account:A").payload
            restored_b = store.get_row("account:B").payload
            restored_item = store.get_row("item:1").payload
        self.assertEqual(restored_a, {"balance": 90, "owner": "Alice"})
        self.assertEqual(restored_b, {"balance": 110, "owner": "Bob"})
        self.assertEqual(restored_item, {"name": "widget", "quantity": 7})

    def test_snapshot_is_self_contained_for_recovery(self) -> None:
        normal = self._run("normal_parallel.json", "original.sqlite3")
        snapshot_path = save_snapshot(
            self.snapshot_dir,
            "self-contained",
            normal,
            initial_sql=INIT_SQL.read_text(encoding="utf-8"),
        )
        # No connection to original.sqlite3 is used for this restore.
        with SQLiteStore(self.temp_dir / "recovered.sqlite3") as store:
            report = restore_snapshot(snapshot_path, store)
            self.assertTrue(report["version_mismatch"])
            self.assertEqual(store.data_version(), normal["final_data_version"])
            self.assertEqual(
                restore_snapshot(snapshot_path, store)["version_mismatch"],
                False,
            )

    def test_same_parameter_replay_is_identical(self) -> None:
        normal = self._run("normal_parallel.json", "normal.sqlite3")
        snapshot_path = save_snapshot(
            self.snapshot_dir,
            "normal-replay",
            normal,
            initial_sql=INIT_SQL.read_text(encoding="utf-8"),
        )
        report = replay_snapshot(snapshot_path)
        self.assertEqual(report["code"], "OK")
        self.assertTrue(report["ok"])
        self.assertFalse(report["parameter_mismatch"])
        self.assertEqual(report["diverged_sessions"], [])
        self.assertTrue(report["event_comparison"]["equal"])
        self.assertEqual(
            report["event_comparison"]["expected_event_count"],
            report["event_comparison"]["actual_event_count"],
        )

    def test_changed_budget_reports_e_replay_with_stable_divergences(self) -> None:
        timeout = self._run("lock_timeout.json", "timeout.sqlite3", budget=40)
        snapshot_path = save_snapshot(
            self.snapshot_dir,
            "timeout-replay",
            timeout,
            initial_sql=INIT_SQL.read_text(encoding="utf-8"),
        )
        first = replay_snapshot(snapshot_path, lock_wait_budget_ms=500)
        second = replay_snapshot(snapshot_path, lock_wait_budget_ms=500)
        self.assertEqual(first["code"], "E_REPLAY")
        self.assertTrue(first["parameter_mismatch"])
        self.assertEqual(first["diverged_sessions"], ["t2"])
        self.assertEqual(first["diverged_sessions"], second["diverged_sessions"])
        self.assertFalse(first["event_comparison"]["equal"])

    def test_deadlock_replay_preserves_victim(self) -> None:
        deadlock = self._run("deadlock.json", "deadlock.sqlite3")
        snapshot_path = save_snapshot(
            self.snapshot_dir,
            "deadlock-replay",
            deadlock,
            initial_sql=INIT_SQL.read_text(encoding="utf-8"),
        )
        report = replay_snapshot(snapshot_path)
        self.assertEqual(report["code"], "OK")
        self.assertEqual(report["diverged_sessions"], [])
        self.assertTrue(report["event_comparison"]["equal"])


if __name__ == "__main__":
    unittest.main()

