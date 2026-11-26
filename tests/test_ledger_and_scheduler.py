from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import load_and_run, run_cli


class LedgerAndSchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.db = Path(cls._temporary.name) / "state.db"
        load_and_run(cls.db)
        completed, cls.status = run_cli(["status", "--db", str(cls.db)])
        assert completed.returncode == 0
        completed, cls.verify = run_cli(["verify", "--db", str(cls.db)])
        assert completed.returncode == 0 and cls.verify["ok"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_no_overselling_and_fixed_point_ledger_fields(self) -> None:
        import sqlite3

        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        try:
            apply_entry = connection.execute(
                "SELECT * FROM ledger_entries "
                "WHERE event_key='apply:1'"
            ).fetchone()
            self.assertEqual(apply_entry["estimated_micros"], 325_000_000)
            self.assertEqual(apply_entry["estimate_scale"], 1_000_000)
            self.assertEqual(apply_entry["actual_scale"], 1_000_000)
            self.assertEqual(apply_entry["rounding_direction"], "up")
            self.assertLessEqual(apply_entry["error_bound_micros"], 1)
            self.assertGreaterEqual(apply_entry["available_after_micros"], 0)
            accepted_sum = connection.execute(
                "SELECT SUM(reserved_micros) FROM applications WHERE state='accepted'"
            ).fetchone()[0]
            self.assertEqual(accepted_sum, self.status["reserved_micros"])
            self.assertLessEqual(
                self.status["occupancy_micros"],
                self.status["global_quota_budget_micros"],
            )
            self.assertLessEqual(
                self.status["aggregate_threshold_micros"],
                self.status["global_quota_budget_micros"],
            )
            releases = connection.execute(
                "SELECT * FROM ledger_entries WHERE event_kind='release' "
                "ORDER BY event_key"
            ).fetchall()
            for release in releases:
                self.assertIsNotNone(release["actual_micros"])
                self.assertGreaterEqual(release["available_after_micros"], 0)
                self.assertGreaterEqual(
                    release["actual_micros"] - release["estimated_micros"],
                    -1_000_000_000,
                )
        finally:
            connection.close()

    def test_drift_threshold_fairness_and_abandonment_reasons(self) -> None:
        import sqlite3

        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        try:
            round5 = connection.execute(
                "SELECT * FROM drift_decisions WHERE round_no=5 "
                "ORDER BY candidate_order"
            ).fetchall()
            self.assertEqual({row["tenant"] for row in round5}, {"t-alpha", "t-beta", "t-gamma"})
            abandoned = [row for row in round5 if row["status"] == "abandoned"]
            self.assertTrue(abandoned)
            self.assertEqual(abandoned[0]["reason"], "round_budget_exhausted")
            self.assertEqual(abandoned[0]["budget_after_micros"], 0)
            round8 = connection.execute(
                "SELECT * FROM drift_decisions WHERE round_no=8 "
                "ORDER BY candidate_order"
            ).fetchall()
            self.assertTrue(round8[0]["dangerous"])
            self.assertLessEqual(
                sum(int(row["applied_delta_micros"]) for row in round5), 90_000_000
            )
            for scheduling in connection.execute(
                "SELECT * FROM scheduling_rounds ORDER BY round_no"
            ):
                self.assertLessEqual(
                    int(scheduling["used_micros"]), int(scheduling["budget_micros"])
                )
                self.assertEqual(
                    int(scheduling["remaining_micros"]),
                    int(scheduling["budget_micros"]) - int(scheduling["used_micros"]),
                )
        finally:
            connection.close()

    def test_peak_shaving_and_threshold_rollback_converge(self) -> None:
        import sqlite3

        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        try:
            release_shave = connection.execute(
                "SELECT * FROM shave_logs WHERE kind='release_shave'"
            ).fetchone()
            self.assertGreater(
                release_shave["original_difference_micros"],
                release_shave["cap_micros"],
            )
            self.assertGreater(release_shave["deferred_micros"], 0)
            drains = connection.execute(
                "SELECT * FROM shave_logs WHERE kind='backlog_drain' "
                "ORDER BY round_no"
            ).fetchall()
            self.assertGreaterEqual(len(drains), 2)
            self.assertEqual(drains[-1]["deferred_micros"], 0)
            alpha_backlog = connection.execute(
                "SELECT shave_backlog_micros, shave_cost_micros FROM tenants "
                "WHERE tenant='t-alpha'"
            ).fetchone()
            self.assertEqual(alpha_backlog["shave_backlog_micros"], 0)
            self.assertGreater(alpha_backlog["shave_cost_micros"], 0)
            rollbacks = connection.execute(
                "SELECT * FROM drift_decisions WHERE fair_tag='rollback' "
                "AND status='applied'"
            ).fetchall()
            self.assertGreaterEqual(len(rollbacks), 2)
            for row in connection.execute("SELECT * FROM shave_logs"):
                self.assertGreaterEqual(row["cost_micros"], 0)
                self.assertGreaterEqual(row["remaining_after_micros"], 0)
                self.assertLessEqual(row["cost_micros"], row["remaining_after_micros"])
                self.assertGreaterEqual(row["safety_watermark_after_micros"], 0)
        finally:
            connection.close()

    def test_cross_version_merge_and_invalidation_closure(self) -> None:
        merge = {row["tenant"]: row for row in self.status["policy_merge"]}
        self.assertEqual(merge["t-alpha"]["source_version"], "v2")
        self.assertEqual(merge["t-beta"]["source_version"], "v2")
        self.assertEqual(merge["t-gamma"]["source_version"], "v2")
        self.assertEqual(merge["t-delta"]["source_version"], "v1")
        invalidated = {
            row["tenant"] for row in self.status["policy_invalidations"]
        }
        self.assertEqual(invalidated, {"t-alpha", "t-beta", "t-delta"})
        assert "t-gamma" not in invalidated
        for tenant in self.status["tenants"]:
            self.assertEqual(tenant["threshold_generation"] >= 1, True)

