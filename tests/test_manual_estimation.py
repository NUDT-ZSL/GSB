from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from patrol_stats.estimation import estimate_query, scan_actual_query
from patrol_stats.model import QuerySpec
from patrol_stats.statistics import validate_summary
from tests.fixtures import make_manual_db


class ManualEstimationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.conn = make_manual_db(Path(self.tempdir.name) / "manual.db")
        self.query = QuerySpec(
            query_id="Q-MANUAL",
            region_id=1,
            band_id=1,
            start_ts="2025-12-31T23:00:00Z",
            end_ts="2026-01-01T13:00:00Z",
            deviation_threshold=0.20,
            workload_class="manual",
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def test_independence_and_combo_estimates_are_independently_checkable(self) -> None:
        actual = scan_actual_query(self.conn, self.query)
        self.assertEqual(actual["base_actual"], 5)
        self.assertEqual(actual["predicate_actuals"], {"region": 6, "band": 7, "time": 12})

        result = estimate_query(self.conn, "v_manual", self.query)
        self.assertAlmostEqual(
            result["independence_base"], 12 * (6 / 12) * (7 / 12), places=6
        )
        self.assertAlmostEqual(result["combo_base"], 5.0, places=6)
        self.assertEqual(result["actual_rows"], 5)
        self.assertEqual(result["first_breach_node"], None)

        base_node = result["nodes"][0]
        self.assertEqual(base_node["node_id"], "N00")
        self.assertAlmostEqual(base_node["selected_estimate"], 5.0, places=6)
        self.assertEqual(base_node["actual_rows"], 5)
        self.assertLess(base_node["relative_error"], 0.20)
        self.assertEqual(base_node["scope"], "combo_histogram:full")

        for node in result["nodes"][1:]:
            self.assertAlmostEqual(node["selected_estimate"], 5.0, places=6)
            self.assertEqual(node["actual_rows"], 5)
            self.assertAlmostEqual(node["overlap_ratio"], 1.0)
            self.assertAlmostEqual(node["right_multiplicity"], 1.0)

    def test_summary_validation_reports_row_count_and_bucket_mismatch(self) -> None:
        self.assertEqual(validate_summary(self.conn, "v_manual"), [])
        self.conn.execute(
            "UPDATE stat_row_counts SET row_count = 99 WHERE version = 'v_manual' AND table_name = 'regions'"
        )
        issues = validate_summary(self.conn, "v_manual")
        self.assertTrue(
            any(
                issue.table_name == "regions"
                and "行数" in issue.reason
                and issue.expected == 99
                for issue in issues
            )
        )

        self.conn.execute(
            "UPDATE stat_row_counts SET row_count = 3 WHERE version = 'v_manual' AND table_name = 'regions'"
        )
        histogram = self.conn.execute(
            """
            SELECT hist_id FROM stat_histograms
            WHERE version = 'v_manual' AND table_name = 'patrol_logs'
              AND column_name = 'region_id'
            """
        ).fetchone()
        self.conn.execute(
            """
            UPDATE stat_histogram_buckets
            SET observed_count = observed_count + 1
            WHERE hist_id = ? AND bucket_no = 0
            """,
            (histogram["hist_id"],),
        )
        issues = validate_summary(self.conn, "v_manual")
        self.assertTrue(
            any(
                issue.table_name == "patrol_logs"
                and issue.column_name == "region_id"
                and "桶" in issue.reason
                for issue in issues
            )
        )


if __name__ == "__main__":
    unittest.main()
