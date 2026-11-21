from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from flowagg import Engine, FlowAggError, load_plan
from tests.helpers import make_plan, record, write_batch, write_manifest


class AggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plan_path = make_plan(
            self.root,
            plan_id="agg-test",
            budget_mib=0.002,
            read_fraction=0.5,
        )
        self.batch1 = write_batch(
            self.root / "b1.jsonl",
            [
                record(1, 1000, byte_count=100, packets=2),
                record(2, 1010, byte_count=300, packets=4),
                record(
                    3,
                    1020,
                    src="lo:127.0.0.1:41002",
                    dst="lo:127.0.0.1:42002",
                    protocol="UDP",
                    direction="egress",
                    byte_count=50,
                    packets=1,
                ),
            ],
        )
        self.batch2 = write_batch(
            self.root / "b2.jsonl",
            [
                record(4, 2000, byte_count=500, packets=8),
                record(
                    5,
                    2010,
                    src="lo:127.0.0.1:41002",
                    dst="lo:127.0.0.1:42002",
                    protocol="UDP",
                    direction="egress",
                    byte_count=70,
                    packets=3,
                ),
            ],
        )
        self.manifest = write_manifest(
            self.root / "manifest.json",
            [("b1", self.batch1), ("b2", self.batch2)],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_spill_truth_and_quantiles(self) -> None:
        engine = Engine(load_plan(self.plan_path), self.root / "state.db")
        result = engine.aggregate_manifest(self.manifest)
        self.assertTrue(result["ok"])
        verify = Engine(load_plan(self.plan_path), self.root / "state.db").verify(
            self.manifest
        )
        self.assertTrue(verify["ok"])
        self.assertEqual(verify["groups_verified"], 2)
        self.assertGreater(verify["status"]["spill"]["files"], 0)
        self.assertGreater(verify["status"]["readback"]["readbacks"], 0)
        self.assertEqual(
            verify["spill_verification"]["complete_groups"],
            verify["groups_verified"],
        )
        intro = next(
            group
            for group in verify["groups"]
            if group["group_key"][1:] == ["TCP", "ingress"]
        )
        self.assertEqual(intro["state"]["count"], 3)
        self.assertEqual(intro["state"]["bytes_sum"], 900)
        self.assertEqual(intro["state"]["packets_sum"], 14)
        self.assertEqual(intro["mean_bytes"], 300.0)
        self.assertEqual(intro["quantiles"]["bytes"]["0.5"]["float"], 300.0)
        self.assertEqual(intro["quantiles"]["bytes"]["0.5"]["fixed"], 300000)
        self.assertEqual(
            intro["quantiles"]["bytes"]["0.5"]["fixed_meta"]["raw_value"], "300"
        )
        byte_quantiles = [
            intro["quantiles"]["bytes"][key]["float"] for key in ("0.5", "0.9")
        ]
        self.assertEqual(byte_quantiles, sorted(byte_quantiles))
        for field in ("bytes", "packets"):
            for result in intro["quantiles"][field].values():
                lower, upper = result["fixed_meta"]["anchor_raw_values"]
                self.assertLessEqual(lower, upper)

    def test_idempotent_duplicate_does_not_increment(self) -> None:
        plan = load_plan(self.plan_path)
        first = Engine(plan, self.root / "state.db").aggregate_path(self.batch1, "b1")
        before = Engine(plan, self.root / "state.db").status()["status"]
        second = Engine(plan, self.root / "state.db").aggregate_path(self.batch1, "b1")
        after = Engine(plan, self.root / "state.db").status()["status"]
        self.assertNotIn("idempotent_replay", first)
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(after["committed_batches"], before["committed_batches"])
        self.assertEqual(after["consumed_record_seq"], before["consumed_record_seq"])
        self.assertEqual(after["spill"]["total_spills"], before["spill"]["total_spills"])

    def test_digest_mismatch_keeps_state(self) -> None:
        plan = load_plan(self.plan_path)
        Engine(plan, self.root / "state.db").aggregate_path(self.batch1, "same-id")
        before = Engine(plan, self.root / "state.db").status()["status"]
        with self.assertRaises(FlowAggError) as caught:
            Engine(plan, self.root / "state.db").aggregate_path(self.batch2, "same-id")
        self.assertEqual(caught.exception.code, "E_DIGEST_MISMATCH")
        after = Engine(plan, self.root / "state.db").status()["status"]
        self.assertEqual(after, before)

    def test_clock_regression_is_distinct_and_atomic(self) -> None:
        plan = load_plan(self.plan_path)
        Engine(plan, self.root / "state.db").aggregate_path(self.batch1, "b1")
        before = Engine(plan, self.root / "state.db").status()["status"]
        late = write_batch(
            self.root / "late.jsonl",
            [record(4, 100, byte_count=1, packets=1)],
        )
        with self.assertRaises(FlowAggError) as caught:
            Engine(plan, self.root / "state.db").aggregate_path(late, "late")
        self.assertEqual(caught.exception.code, "E_CLOCK_REGRESSION")
        after = Engine(plan, self.root / "state.db").status()["status"]
        self.assertEqual(after, before)

    def test_verify_reports_first_divergence(self) -> None:
        plan = load_plan(self.plan_path)
        Engine(plan, self.root / "state.db").aggregate_manifest(self.manifest)
        conn = sqlite3.connect(self.root / "state.db")
        conn.execute(
            "UPDATE groups SET bytes_sum=bytes_sum+1 WHERE group_key_json LIKE '%TCP%'"
        )
        conn.commit()
        conn.close()
        with self.assertRaises(FlowAggError) as caught:
            Engine(plan, self.root / "state.db").verify(self.manifest)
        self.assertEqual(caught.exception.code, "E_VERIFY_MISMATCH")
        divergence = caught.exception.details["first_divergence"]
        self.assertEqual(divergence["field"], "bytes_sum")
        self.assertIn("database_state", divergence)
        self.assertIn("truth_state", divergence)
        self.assertIn("spill_chain", divergence)


if __name__ == "__main__":
    unittest.main()

