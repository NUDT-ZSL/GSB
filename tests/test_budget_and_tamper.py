from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from flowagg import Engine, FlowAggError, load_plan
from tests.helpers import make_plan, record, write_batch, write_manifest


class BudgetAndTamperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_single_group_too_large_preserves_prior_commit(self) -> None:
        plan_path = make_plan(
            self.root,
            plan_id="budget-test",
            budget_mib=0.001,
            dimensions=["endpoint_pair"],
        )
        plan = load_plan(plan_path)
        good = write_batch(
            self.root / "good.jsonl",
            [
                record(
                    1,
                    100,
                    src="lo-a",
                    dst="lo-b",
                    byte_count=10,
                    packets=1,
                )
            ],
        )
        Engine(plan, self.root / "state.db").aggregate_path(good, "good")
        before = Engine(plan, self.root / "state.db").status()["status"]
        before_meta = sqlite3.connect(self.root / "state.db").execute(
            "SELECT value FROM meta WHERE key='next_batch_order'"
        ).fetchone()[0]
        huge_a = "lo-" + "a" * 245
        huge_b = "lo-" + "b" * 245
        bad = write_batch(
            self.root / "too-big.jsonl",
            [
                record(
                    2,
                    200,
                    src=huge_a,
                    dst=huge_b,
                    byte_count=10,
                    packets=1,
                )
            ],
        )
        with self.assertRaises(FlowAggError) as caught:
            Engine(plan, self.root / "state.db").aggregate_path(bad, "too-big")
        self.assertEqual(caught.exception.code, "E_SINGLE_GROUP_TOO_LARGE")
        after = Engine(plan, self.root / "state.db").status()["status"]
        self.assertEqual(after["committed_batches"], before["committed_batches"])
        self.assertEqual(after["group_count"], before["group_count"])
        self.assertEqual(after["readback"], before["readback"])
        after_meta = sqlite3.connect(self.root / "state.db").execute(
            "SELECT value FROM meta WHERE key='next_batch_order'"
        ).fetchone()[0]
        self.assertEqual(after_meta, before_meta)

    def test_read_buffer_deferral_is_explicit_and_exact(self) -> None:
        plan_path = make_plan(
            self.root,
            plan_id="read-buffer-test",
            budget_mib=0.001,
            dimensions=["endpoint_pair"],
        )
        plan = load_plan(plan_path)
        first = write_batch(
            self.root / "b1.jsonl",
            [
                record(1, 100, src="lo-a", dst="lo-b", byte_count=10),
                record(2, 110, src="lo-c", dst="lo-d", byte_count=20),
            ],
        )
        second = write_batch(
            self.root / "b2.jsonl",
            [
                record(3, 200, src="lo-a", dst="lo-b", byte_count=30),
            ],
        )
        Engine(plan, self.root / "state.db").aggregate_path(first, "b1")
        Engine(plan, self.root / "state.db").aggregate_path(second, "b2")
        status = Engine(plan, self.root / "state.db").status()["status"]
        self.assertGreater(status["readback"]["deferred"], 0)
        self.assertEqual(
            status["readback"]["defer_reasons"].get("READ_BUFFER_TOO_SMALL"), 1
        )
        group = next(
            group
            for group in Engine(plan, self.root / "state.db").report_groups()
            if group["group_key"] == ["lo-a->lo-b"]
        )
        self.assertEqual(group["state"]["count"], 2)
        self.assertEqual(group["state"]["bytes_sum"], 40)

    def test_tampered_spill_is_detected(self) -> None:
        plan_path = make_plan(self.root, plan_id="tamper-test", budget_mib=0.004)
        plan = load_plan(plan_path)
        rows = [
            record(
                index,
                1000 + index,
                src=f"lo:127.0.0.1:{41000 + index}",
                dst=f"lo:127.0.0.1:{42000 + index}",
                byte_count=100 + index,
            )
            for index in range(1, 7)
        ]
        batch = write_batch(self.root / "b1.jsonl", rows)
        manifest = write_manifest(self.root / "manifest.json", [("b1", batch)])
        Engine(plan, self.root / "state.db").aggregate_manifest(manifest)
        spill_file = sorted((self.root / "spill").glob("spill-*.json"))[0]
        spill_file.write_bytes(spill_file.read_bytes() + b" ")
        with self.assertRaises(FlowAggError) as caught:
            Engine(plan, self.root / "state.db").verify(manifest)
        self.assertEqual(caught.exception.code, "E_SPILL_TAMPERED")
        self.assertIn("path", caught.exception.details)


if __name__ == "__main__":
    unittest.main()

