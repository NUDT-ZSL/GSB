from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from water_quality.engine import PushdownEngine
from water_quality.generator import GeneratorConfig, generate_database


class EngineEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "engine.db"
        cls.generated = generate_database(
            cls.db,
            GeneratorConfig(
                seed=123,
                pipe_count=8,
                point_count=16,
                time_steps=10,
                null_rate=0.18,
                null_burst_rate=0.05,
            ),
        )
        with PushdownEngine(str(cls.db)) as engine:
            cls.full = engine.run_all(run_id="test-full")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_every_query_matches_central_three_valued_execution(self) -> None:
        self.assertTrue(self.full["all_consistent"])
        self.assertEqual(self.full["query_count"], 7)
        for report in self.full["query_reports"]:
            self.assertTrue(report["is_consistent"], report)
            self.assertEqual(
                report["downpush_count"], report["central_count"], report
            )
            self.assertEqual(report["marker_mismatches"], [])
            self.assertEqual(report["order_mismatches"], [])

    def test_audit_lists_match_generated_truth(self) -> None:
        audit = self.full["audit"]
        self.assertTrue(audit["violations_match_truth"])
        self.assertTrue(audit["reviews_match_truth"])
        self.assertTrue(audit["violation_partition_ids_match_truth"])
        self.assertTrue(audit["review_partition_ids_match_truth"])
        self.assertEqual(
            audit["violation_count"], self.generated.violation_rows
        )
        self.assertEqual(audit["review_count"], self.generated.review_rows)

    def test_query_rows_match_persisted_generated_truth(self) -> None:
        with PushdownEngine(str(self.db)) as engine:
            query_ids = [
                row["query_id"]
                for row in engine.conn.execute(
                    "SELECT query_id FROM query_requests ORDER BY query_id"
                )
            ]
            for query_id in query_ids:
                actual = [
                    row["row_signature"]
                    for row in engine.conn.execute(
                        """
                        SELECT row_signature
                        FROM run_query_results
                        WHERE run_id = 'test-full' AND query_id = ?
                        ORDER BY row_index
                        """,
                        (query_id,),
                    )
                ]
                expected = [
                    row["row_signature"]
                    for row in engine.conn.execute(
                        """
                        SELECT row_signature
                        FROM truth_query_results
                        WHERE query_id = ?
                        ORDER BY row_index
                        """,
                        (query_id,),
                    )
                ]
                self.assertEqual(actual, expected, query_id)

    def test_outer_join_marks_both_null_key_sources(self) -> None:
        with PushdownEngine(str(self.db)) as engine:
            rows = [
                dict(row)
                for row in engine.conn.execute(
                    """
                    SELECT row_json
                    FROM run_query_results
                    WHERE run_id = 'test-full' AND query_id = 'Q007'
                    ORDER BY row_index
                    """
                )
            ]
        import json

        decoded = [json.loads(row["row_json"]) for row in rows]
        markers = {row["join_null_source"] for row in decoded}
        self.assertIn("point_key_null", markers)
        self.assertIn("pipe_key_null", markers)
        self.assertIn("none", markers)

    def test_inner_join_drops_null_keys_while_full_join_keeps_them(self) -> None:
        with PushdownEngine(str(self.db)) as engine:
            inner = engine.execute_pushdown_query("Q001")
            full = engine.execute_pushdown_query("Q007")
        self.assertTrue(inner["consistency"]["is_consistent"])
        self.assertTrue(full["consistency"]["is_consistent"])
        self.assertFalse(
            any(
                row["join_null_source"] == "pipe_key_null"
                for row in inner["rows"]
            )
        )

    def test_null_verdicts_are_explained_as_unknown(self) -> None:
        with PushdownEngine(str(self.db)) as engine:
            rows = engine.conn.execute(
                """
                SELECT predicate_sql, field_name, verdict
                FROM run_null_verdicts
                WHERE run_id = 'test-full'
                LIMIT 20
                """
            ).fetchall()
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertEqual(row["verdict"], "UNKNOWN")
            self.assertIn("IS NOT NULL", row["predicate_sql"])
            self.assertIn(
                row["field_name"],
                {
                    "residual_chlorine",
                    "turbidity",
                    "ph",
                    "water_temp",
                },
            )

    def test_threshold_equality_is_not_a_violation(self) -> None:
        with PushdownEngine(str(self.db)) as engine:
            rows = engine.conn.execute(
                """
                SELECT metric, value
                FROM truth_violations
                WHERE (metric = 'residual_chlorine' AND value = 0.2)
                   OR (metric = 'turbidity' AND value = 1.0)
                   OR (metric = 'ph' AND (value = 6.5 OR value = 8.5))
                """
            ).fetchall()
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()

