from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlplan_lab.anomalies import run_all_anomalies, run_anomaly
from sqlplan_lab.engine import Engine
from sqlplan_lab.errors import (
    E_DATA,
    E_PARSE,
    E_REPLAY,
    E_SKEW,
    E_SNAPSHOT,
    E_STATS,
    E_VERSION,
    LabError,
)


ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "demo.sql"
QUERY = (ROOT / "samples" / "query.sql").read_text(encoding="utf-8")
REFORMATTED = """
  select O.ID , o.amount,c.region
  from ORDERS as O
  join CUSTOMERS as C on (O.customer_id=C.id)
  where ((O.status='paid') and (O.amount>=100));
"""


class LabTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "lab.sqlite3"
        self.engine = Engine(self.db_path)
        self.addCleanup(self.engine.close)
        self.ingest = self.engine.ingest_sql_file(SAMPLE)

    def assert_code(self, code: str, fn, *args, **kwargs):
        with self.assertRaises(LabError) as context:
            fn(*args, **kwargs)
        self.assertEqual(context.exception.code, code)
        return context.exception

    def stats(self, buckets: int = 8, rate: float = 1.0, overrides=None):
        return self.engine.create_stats_version(buckets, rate, overrides or {})[
            "stats_version"
        ]

    def test_plan_normalization_stable_topology_and_parse_rejections(self) -> None:
        first = self.engine.build_plan(QUERY)
        second = self.engine.build_plan(REFORMATTED)
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(first["normalized_sql"], second["normalized_sql"])
        self.assertEqual(first["topological_order"], second["topological_order"])
        self.assertEqual(first["tree"], second["tree"])
        self.assertEqual(
            [node["kind"] for node in first["nodes"]],
            ["scan", "scan", "join", "filter"],
        )
        self.assert_code(
            E_PARSE,
            self.engine.build_plan,
            "SELECT o.id FROM orders o WHERE o.id IN (SELECT id FROM customers)",
        )
        self.assert_code(E_PARSE, self.engine.build_plan, "SELECT COUNT(*) FROM orders")
        self.assert_code(
            E_PARSE,
            self.engine.build_plan,
            "SELECT o.id FROM orders o GROUP BY o.id",
        )

    def test_ingestion_is_deterministic_and_failure_rolls_back(self) -> None:
        same = self.engine.ingest_sql_file(SAMPLE)
        self.assertEqual(self.ingest["data_version"], same["data_version"])
        self.assertEqual(self.ingest["digest"], same["digest"])
        before_version = self.engine.current_data_version
        before_orders = self.engine.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        broken = """
        CREATE TABLE broken (id INTEGER PRIMARY KEY);
        INSERT INTO broken VALUES (1);
        INSERT INTO missing_table VALUES (1);
        """
        self.assert_code(E_DATA, self.engine.ingest_sql_text, broken)
        self.assertEqual(self.engine.current_data_version, before_version)
        self.assertEqual(
            self.engine.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            before_orders,
        )
        tables = {item["table"] for item in self.engine.list_tables()}
        self.assertEqual(tables, {"orders", "customers"})

    def test_estimates_are_deterministic_and_missing_stats_is_e_stats(self) -> None:
        stats_version = self.stats(4, 0.5)
        first = self.engine.estimate(QUERY, stats_version)["estimate"]
        second = self.engine.estimate(QUERY, stats_version)["estimate"]
        self.assertEqual(first, second)
        self.assertEqual(first["stats_version"], stats_version)
        self.assertTrue(all(isinstance(item["estimated_rows"], int) for item in first["operators"]))
        self.assert_code(E_STATS, self.engine.estimate, QUERY, "sv_missing")
        self.assert_code(E_STATS, self.engine.analyze, QUERY, "sv_missing")

    def test_attribution_and_zero_actual_sentinel(self) -> None:
        stats_version = self.stats()
        analysis = self.engine.analyze(QUERY, stats_version)
        self.assertEqual(len(analysis["operators"]), 4)
        self.assertEqual(len(analysis["attribution_chain"]), 4)
        self.assertEqual(analysis["data_version"], self.engine.current_data_version)
        self.assertIsNotNone(analysis["primary_operator_id"])
        for row in analysis["operators"]:
            self.assertIsInstance(row["estimated_rows"], int)
            self.assertIsInstance(row["actual_rows"], int)
            self.assertIsInstance(row["deviation_score"], int)
        zero_query = (
            "SELECT o.id FROM orders o JOIN customers c ON o.customer_id=c.id "
            "WHERE o.status = 'never'"
        )
        zero = self.engine.analyze(zero_query, stats_version)
        root = zero["operators"][-1]
        self.assertEqual(root["actual_rows"], 0)
        self.assertEqual(root["estimated_to_actual_ratio"], "ZERO_ACTUAL")
        self.assertEqual(root["zero_actual_sentinel"], "ZERO_ACTUAL")
        self.assertIsNone(root["ratio_ppm"])

    def test_in_or_and_range_predicates(self) -> None:
        stats_version = self.stats(6, 1.0)
        query = (
            "SELECT o.id FROM orders o WHERE "
            "(o.status IN ('paid', 'pending') AND o.amount >= 100) "
            "OR o.amount < 75"
        )
        plan = self.engine.build_plan(query)
        self.assertEqual([node["kind"] for node in plan["nodes"]], ["scan", "filter"])
        first = self.engine.analyze(query, stats_version)
        second = self.engine.analyze(query, stats_version)
        self.assertEqual(first["operators"], second["operators"])
        self.assertGreaterEqual(first["operators"][-1]["actual_rows"], 1)
        self.assertTrue(all(item["estimated_rows"] >= 0 for item in first["operators"]))

    def test_heatmap_whole_replacement_and_version_comparison(self) -> None:
        old_stats = self.stats(4, 0.5)
        old_analysis = self.engine.analyze(QUERY, old_stats)
        self.assertEqual(len(self.engine.load_heatmap()), len(old_analysis["operators"]))
        other_query = "SELECT o.id FROM orders o WHERE o.status = 'paid'"
        other = self.engine.analyze(other_query, old_stats)
        other_heat = self.engine.load_heatmap()
        self.assertEqual(len(other_heat), len(other["operators"]))
        self.assertEqual(
            {row["op_id"] for row in other_heat},
            {row["op_id"] for row in other["operators"]},
        )
        new_stats = self.stats(12, 1.0, {"orders.status": 0.12, "orders.amount": 0.05})
        self.assertNotEqual(old_stats, new_stats)
        new_analysis = self.engine.analyze(QUERY, new_stats)
        new_heat = self.engine.load_heatmap()
        self.assertEqual(len(new_heat), len(new_analysis["operators"]))
        self.assertTrue(all("statistics" in row for row in new_heat))
        self.assertTrue(
            any(item["stats_version"] == old_stats for item in self.engine.list_stats_versions())
        )
        comparison = self.engine.compare_stats_versions(QUERY, old_stats, new_stats)
        self.assertTrue(comparison["versions_differ"])
        self.assertGreater(comparison["changed_operator_count"], 0)
        plan = self.engine.build_plan(QUERY)
        changed_ids = [row["op_id"] for row in comparison["changed_operators"]]
        self.assertEqual(changed_ids, [op_id for op_id in plan["topological_order"] if op_id in changed_ids])

    def test_stale_statistics_return_e_version(self) -> None:
        old_stats = self.stats()
        changed = SAMPLE.read_text(encoding="utf-8").replace("(101, 1, 'paid',     420", "(101, 1, 'paid',     421")
        self.engine.ingest_sql_text(changed)
        self.assertNotEqual(self.engine.current_data_version, self.ingest["data_version"])
        self.assert_code(E_VERSION, self.engine.estimate, QUERY, old_stats)
        self.assert_code(E_VERSION, self.engine.analyze, QUERY, old_stats)

    def test_snapshot_restore_replaces_state_and_replays_history(self) -> None:
        old_stats = self.stats(4, 0.5)
        self.engine.analyze(QUERY, old_stats)
        new_stats = self.stats(12, 1.0, {"orders.status": 0.12})
        self.engine.analyze(QUERY, new_stats)
        saved = self.engine.save_snapshot("baseline")
        self.assertEqual(saved["plan_count"], 2)
        # Mutate current state after the snapshot; restore must not merge it.
        self.engine.analyze(QUERY, old_stats)
        self.assertEqual(len(self.engine.session_state()["plan_history"]), 3)
        self.engine.close()
        self.engine = Engine(self.db_path)
        restored = self.engine.restore_snapshot("baseline")
        self.assertTrue(restored["replaced"])
        self.assertEqual(len(self.engine.session_state()["plan_history"]), 2)
        self.assertEqual(self.engine.session_state()["stats_version"], new_stats)
        self.assertTrue(all(item["same"] for item in restored["history_verification"]))
        self.assertTrue(all(item["same"] for item in self.engine.verify_restored_history()))
        self.engine.save_snapshot("with_missing_stats")
        self.engine.conn.execute("DELETE FROM _lab_stats WHERE version = ?", (new_stats,))
        self.assert_code(E_SNAPSHOT, self.engine.restore_snapshot, "with_missing_stats")

    def test_snapshot_missing_current_stats_without_history_is_e_snapshot(self) -> None:
        stats_version = self.stats()
        self.engine.save_snapshot("empty_history")
        self.engine.conn.execute("DELETE FROM _lab_stats WHERE version = ?", (stats_version,))
        self.assert_code(E_SNAPSHOT, self.engine.restore_snapshot, "empty_history")

    def test_three_anomalies_and_stable_logs(self) -> None:
        stats_version = self.stats()
        self.engine.analyze(QUERY, stats_version)
        before = self.engine.current_data_version
        reports = run_all_anomalies(self.engine, QUERY, stats_version)
        self.assertEqual(
            [item["business_code"] for item in reports],
            [E_SKEW, E_STATS, E_REPLAY],
        )
        logs_before = self.engine.list_logs()
        reports_again = run_all_anomalies(self.engine, QUERY, stats_version)
        logs_after = self.engine.list_logs()
        self.assertEqual(
            [item["log_event_id"] for item in reports],
            [item["log_event_id"] for item in reports_again],
        )
        self.assertEqual(logs_before, logs_after)
        self.assertEqual(len(logs_after), 3)
        self.assertEqual(
            logs_after,
            sorted(logs_after, key=lambda item: (item["event"], item["event_id"], item["event_key"])),
        )
        self.assertEqual(self.engine.current_data_version, before)


if __name__ == "__main__":
    unittest.main()
