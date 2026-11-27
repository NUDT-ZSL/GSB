from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from water_quality.engine import PushdownEngine
from water_quality.generator import GeneratorConfig, generate_database


class ManualBoundarySampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "boundary.db"
        generate_database(
            self.db,
            GeneratorConfig(
                seed=77, pipe_count=8, point_count=12, time_steps=5
            ),
        )
        with PushdownEngine(str(self.db)) as engine:
            engine.conn.execute(
                """
                INSERT INTO pipes(
                    pipe_id, upstream_node, downstream_node, region,
                    diameter_mm, material, partition_id
                ) VALUES (
                    'P999', 'NBOUND-IN', 'NBOUND-OUT', 'boundary',
                    200, 'steel', 'pipe:region:boundary'
                )
                """
            )
            engine.conn.execute(
                """
                INSERT INTO monitoring_points(
                    point_id, pipe_id, location, metric_types, partition_id
                ) VALUES (
                    'M999', 'P999', 'boundary-01',
                    'residual_chlorine,turbidity,ph', 'point:zone:boundary'
                )
                """
            )
            samples = (
                ("2026-12-01T00:00:00Z", 0.2, None, None),
                ("2026-12-01T01:00:00Z", 0.199, None, None),
                ("2026-12-01T02:00:00Z", None, 1.0, None),
                ("2026-12-01T03:00:00Z", None, 1.001, None),
                ("2026-12-01T04:00:00Z", None, None, 6.5),
                ("2026-12-01T05:00:00Z", None, None, 6.499),
                ("2026-12-01T06:00:00Z", None, None, 8.5),
                ("2026-12-01T07:00:00Z", None, None, 8.501),
                ("2026-12-01T08:00:00Z", None, None, None),
            )
            engine.conn.executemany(
                """
                INSERT INTO readings(
                    point_id, sampled_at, residual_chlorine,
                    turbidity, ph, water_temp
                ) VALUES ('M999', ?, ?, ?, ?, 20.0)
                """,
                samples,
            )
            engine.conn.commit()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _query(
        self,
        engine: PushdownEngine,
        query_id: str,
        groups: list[list[dict[str, object]]],
    ) -> dict:
        query = {
            "query_id": query_id,
            "description": "manual boundary sample",
            "join_type": "inner",
            "pipe_predicates": [
                {"field": "pipe_id", "operator": "=", "value": "P999"}
            ],
            "reading_predicate_groups": groups,
        }
        result = engine.execute_pushdown_query(query)
        self.assertTrue(result["consistency"]["is_consistent"], result)
        return result

    def test_threshold_equality_null_and_out_of_range_boundaries(self) -> None:
        with PushdownEngine(str(self.db)) as engine:
            chlorine = self._query(
                engine,
                "QBC",
                [[{"field": "residual_chlorine", "operator": "<", "value": 0.2}]],
            )
            turbidity = self._query(
                engine,
                "QBT",
                [[{"field": "turbidity", "operator": ">", "value": 1.0}]],
            )
            ph = self._query(
                engine,
                "QBP",
                [
                    [{"field": "ph", "operator": "<", "value": 6.5}],
                    [{"field": "ph", "operator": ">", "value": 8.5}],
                ],
            )

        self.assertEqual(
            [row["sampled_at"] for row in chlorine["rows"]],
            ["2026-12-01T01:00:00Z"],
        )
        self.assertEqual(
            [row["sampled_at"] for row in turbidity["rows"]],
            ["2026-12-01T03:00:00Z"],
        )
        self.assertEqual(
            [row["sampled_at"] for row in ph["rows"]],
            ["2026-12-01T05:00:00Z", "2026-12-01T07:00:00Z"],
        )
        self.assertTrue(
            any(
                item["source_type"] == "reading"
                and item["field_name"] == "residual_chlorine"
                and item["predicate_id"] == "QBC.G01.READING.01"
                for item in chlorine["null_verdicts"]
            )
        )


if __name__ == "__main__":
    unittest.main()

