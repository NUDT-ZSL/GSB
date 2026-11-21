from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowagg import Engine, FlowAggError, load_plan
from tests.helpers import make_plan, record, stable_json, write_batch


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plan_path = make_plan(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_error(self, code: str, row: object) -> None:
        batch_path = self.root / "bad.jsonl"
        batch_path.write_text(stable_json(row) + "\n", encoding="utf-8")
        plan = load_plan(self.plan_path)
        engine = Engine(plan, self.root / "state.db")
        with self.assertRaises(FlowAggError) as caught:
            engine.aggregate_path(batch_path, "bad")
        self.assertEqual(caught.exception.code, code)
        self.assertFalse((self.root / "state.db").exists())

    def test_rejects_negative_and_non_integer_values(self) -> None:
        self.assert_error("E_RECORD_INVALID", record(1, 100, byte_count=-1))
        self.assert_error("E_RECORD_INVALID", record(1, 100, packets="2"))
        self.assert_error("E_RECORD_INVALID", record(-1, 100))

    def test_rejects_nan_and_infinite_values(self) -> None:
        self.assert_error(
            "E_NAN_OR_OVERFLOW",
            {
                **record(1, 100),
                "bytes": float("nan"),
            },
        )
        text = stable_json(record(1, 100)).replace('"bytes":100', '"bytes":1e400')
        batch_path = self.root / "bad.jsonl"
        batch_path.write_text(text + "\n", encoding="utf-8")
        with self.assertRaises(FlowAggError) as caught:
            Engine(load_plan(self.plan_path), self.root / "state.db").aggregate_path(
                batch_path, "bad"
            )
        self.assertEqual(caught.exception.code, "E_NAN_OR_OVERFLOW")
        self.assertFalse((self.root / "state.db").exists())

    def test_rejects_endpoint_protocol_and_direction(self) -> None:
        self.assert_error(
            "E_ENDPOINT_INVALID",
            record(1, 100, src="tcp://10.0.0.1:80"),
        )
        self.assert_error("E_PROTOCOL_INVALID", record(1, 100, protocol="FTP"))
        self.assert_error("E_DIRECTION_INVALID", record(1, 100, direction="sideways"))

    def test_rejects_non_increasing_sequence(self) -> None:
        rows = [record(2, 100), record(2, 110)]
        batch_path = self.root / "bad.jsonl"
        write_batch(batch_path, rows)
        with self.assertRaises(FlowAggError) as caught:
            Engine(load_plan(self.plan_path), self.root / "state.db").aggregate_path(
                batch_path, "bad"
            )
        self.assertEqual(caught.exception.code, "E_RECORD_SEQ")
        self.assertFalse((self.root / "state.db").exists())

    def test_missing_sample_is_distinct(self) -> None:
        with self.assertRaises(FlowAggError) as caught:
            Engine(load_plan(self.plan_path), self.root / "state.db").aggregate_path(
                self.root / "does-not-exist.jsonl", "missing"
            )
        self.assertEqual(caught.exception.code, "E_SAMPLE_MISSING")
        self.assertFalse((self.root / "state.db").exists())

    def test_invalid_plan_does_not_create_a_store(self) -> None:
        bad_plan = self.root / "bad-plan.json"
        bad_plan.write_text(json.dumps({"plan_id": "x"}), encoding="utf-8")
        with self.assertRaises(FlowAggError) as caught:
            load_plan(bad_plan)
        self.assertEqual(caught.exception.code, "E_PLAN_INVALID")
        self.assertFalse((self.root / "state.db").exists())

    def test_aggregate_overflow_aborts_the_whole_batch(self) -> None:
        rows = [
            record(1, 100, byte_count=3_000_000_000, packets=1),
            record(2, 110, byte_count=3_000_000_000, packets=1),
        ]
        batch_path = write_batch(self.root / "overflow.jsonl", rows)
        plan = load_plan(self.plan_path)
        with self.assertRaises(FlowAggError) as caught:
            Engine(plan, self.root / "state.db").aggregate_path(
                batch_path, "overflow"
            )
        self.assertEqual(caught.exception.code, "E_OVERFLOW")
        status = Engine(plan, self.root / "state.db").status()["status"]
        self.assertEqual(status["committed_batches"], 0)
        self.assertEqual(status["processing_batches"], 0)
        self.assertEqual(status["group_count"], 0)


if __name__ == "__main__":
    unittest.main()

