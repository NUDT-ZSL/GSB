from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from water_quality.engine import (
    PartitionVersionMismatchError,
    PushdownEngine,
)
from water_quality.db import metadata_get
from water_quality.generator import GeneratorConfig, generate_database
from water_quality.rules import PartitionRules
from water_quality.snapshots import (
    create_snapshot,
    resume_snapshot,
    verify_snapshot_digest,
)


class IncrementalAndSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "water.db"
        generate_database(
            self.db,
            GeneratorConfig(
                seed=321,
                pipe_count=8,
                point_count=16,
                time_steps=10,
                null_rate=0.16,
                null_burst_rate=0.04,
            ),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _query(self, conn, query_id: str) -> dict:
        return json.loads(
            conn.execute(
                "SELECT request_json FROM query_requests WHERE query_id = ?",
                (query_id,),
            ).fetchone()["request_json"]
        )

    def test_predicate_change_recomputes_only_affected_query_partitions(self) -> None:
        with PushdownEngine(str(self.db)) as engine:
            full = engine.run_all(run_id="inc-full")
            query = self._query(engine.conn, "Q001")
            query["reading_predicate_groups"][0][0]["value"] = 0.5
            incremental = engine.run_incremental(
                full["run_id"],
                query_changes={"Q001": query},
                run_id="inc-one",
            )
            checks = {
                item["query_id"]: item
                for item in incremental["incremental_checks"]
            }
            self.assertTrue(incremental["all_consistent"])
            self.assertTrue(incremental["all_incremental_consistent"])
            self.assertTrue(
                checks["Q001"]["recomputed_partition_ids"],
                "changed query should have affected partitions",
            )
            for query_id in ("Q002", "Q003", "Q004", "Q005", "Q006", "Q007"):
                self.assertEqual(
                    checks[query_id]["recomputed_partition_ids"], []
                )
                self.assertGreater(
                    checks[query_id]["reused_join_task_count"], 0
                )

            # A separate full run over the same changed query must have exactly
            # the same rows in exactly the same order.
            separate = engine.run_all(run_id="inc-separate-full")
            self.assertTrue(separate["all_consistent"])
            for query_id in ("Q001", "Q002", "Q003", "Q004", "Q005", "Q006", "Q007"):
                incremental_rows = [
                    row["row_signature"]
                    for row in engine.conn.execute(
                        """
                        SELECT row_signature FROM run_query_results
                        WHERE run_id = ? AND query_id = ?
                        ORDER BY row_index
                        """,
                        (incremental["run_id"], query_id),
                    )
                ]
                full_rows = [
                    row["row_signature"]
                    for row in engine.conn.execute(
                        """
                        SELECT row_signature FROM run_query_results
                        WHERE run_id = ? AND query_id = ?
                        ORDER BY row_index
                        """,
                        (separate["run_id"], query_id),
                    )
                ]
                self.assertEqual(incremental_rows, full_rows)

    def test_partition_rule_change_is_incremental_and_verified(self) -> None:
        with PushdownEngine(str(self.db)) as engine:
            full = engine.run_all(run_id="rule-full")
            rules = PartitionRules(
                version="partition-test-v2",
                pipe_strategy="hash",
                point_strategy="hash",
                pipe_hash_buckets=2,
                point_hash_buckets=2,
            )
            result = engine.run_incremental(
                full["run_id"],
                new_rules=rules,
                run_id="rule-inc",
            )
            self.assertEqual(
                result["partition_rules_version"], "partition-test-v2"
            )
            self.assertTrue(result["all_consistent"])
            self.assertTrue(result["all_incremental_consistent"])
            for check in result["incremental_checks"]:
                self.assertTrue(check["recomputed_partition_ids"])

    def test_snapshot_resume_and_digest(self) -> None:
        with PushdownEngine(str(self.db)) as engine:
            full = engine.run_all(run_id="snap-full")
            snapshot = create_snapshot(
                engine.conn, full["run_id"], snapshot_id="snap-test"
            )
            self.assertEqual(snapshot["query_count"], 7)
            self.assertTrue(snapshot["all_consistent"])
            self.assertEqual(
                snapshot["data_fingerprint"],
                metadata_get(engine.conn, "data_fingerprint"),
            )
            digest = verify_snapshot_digest(engine.conn, "snap-test")
            self.assertTrue(digest["valid"])

            query = self._query(engine.conn, "Q002")
            query["reading_predicate_groups"][0][0]["value"] = 1.2
            resumed = resume_snapshot(
                engine,
                "snap-test",
                query_changes={"Q002": query},
                run_id="snap-resume",
            )
            self.assertEqual(resumed["resumed_from_snapshot"], "snap-test")
            self.assertTrue(resumed["all_consistent"])
            self.assertTrue(resumed["all_incremental_consistent"])
            self.assertTrue(resumed["audit"]["violations_match_truth"])

    def test_resume_rejects_partition_rule_version_change(self) -> None:
        with PushdownEngine(str(self.db)) as engine:
            full = engine.run_all(run_id="version-full")
            create_snapshot(
                engine.conn, full["run_id"], snapshot_id="version-snapshot"
            )
            new_rules = PartitionRules(
                version="partition-test-v3",
                pipe_strategy="hash",
                point_strategy="location_zone",
                pipe_hash_buckets=3,
            )
            engine.rules = new_rules
            from water_quality.rules import activate_partition_rules

            activate_partition_rules(engine.conn, new_rules)
            with self.assertRaises(PartitionVersionMismatchError) as context:
                resume_snapshot(
                    engine,
                    "version-snapshot",
                    run_id="should-not-exist",
                )
            message = str(context.exception)
            self.assertIn("拒绝续算", message)
            self.assertIn("partition-test-v3", message)
            self.assertIn("partition-v1", message)
            self.assertIsNone(
                engine.conn.execute(
                    "SELECT 1 FROM runs WHERE run_id = 'should-not-exist'"
                ).fetchone()
            )


if __name__ == "__main__":
    unittest.main()

