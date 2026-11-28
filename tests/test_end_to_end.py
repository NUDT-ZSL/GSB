from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from patrol_stats.advisor import apply_update_plan, generate_update_plan
from patrol_stats.db import connect, initialize_schema
from patrol_stats.evaluation import (
    compare_versions,
    evaluate_workload,
    list_snapshots,
)
from patrol_stats.generator import generate_dataset
from patrol_stats.statistics import build_statistics, validate_summary


class EndToEndTests(unittest.TestCase):
    def test_update_reduces_error_and_versions_are_comparable(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "patrol.db"
            generate_dataset(
                db_path,
                root / "generated",
                seed=20261128,
                log_count=1000,
                query_count=12,
            )
            conn = connect(db_path)
            initialize_schema(conn)
            try:
                build_statistics(
                    conn,
                    "v1_initial",
                    sample_size=128,
                    combo_sample_size=32,
                    bucket_count=8,
                    combo_source="sample",
                )
                self.assertEqual(validate_summary(conn, "v1_initial"), [])
                before = evaluate_workload(conn, "v1_initial", note="before")
                plan = generate_update_plan(conn, before["run_id"])
                self.assertGreaterEqual(len(plan["suggestions"]), 1)
                applied = apply_update_plan(
                    conn, plan["plan_id"], sample_size=512
                )
                self.assertTrue(applied["converged"])
                self.assertLess(applied["mean_after"], applied["mean_before"])
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT source FROM stat_combo_histograms
                        WHERE version = ?
                        """,
                        (applied["target_version"],),
                    ).fetchone()[0],
                    "full",
                )

                runs = list_snapshots(conn)
                self.assertEqual(len(runs), 2)
                first_query = before["queries"][0]["query_id"]
                comparison = compare_versions(
                    conn,
                    first_query,
                    "v1_initial",
                    applied["target_version"],
                )
                self.assertEqual(comparison["actual_rows"] > 0, True)
                self.assertEqual(len(comparison["node_deltas"]), len(before["queries"][0]["join_order"]) + 1)
                self.assertTrue(
                    any(
                        delta["from_estimate"] != delta["to_estimate"]
                        for delta in comparison["node_deltas"]
                    )
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
