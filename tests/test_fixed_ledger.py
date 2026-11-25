from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from decimal import Decimal
from pathlib import Path

from batchledger.fixed import FixedLedger
from tests.support import cli

ROOT = Path(__file__).resolve().parents[1]


class FixedLedgerTests(unittest.TestCase):
    def test_quantization_add_and_multiply_bounds(self) -> None:
        ledger = FixedLedger("half_up")
        first = ledger.quantize("first", "first", 1.2345, 100)
        second = ledger.quantize("second", "second", 2.3456, 100)
        total = ledger.add("sum", "sum", (first, second), 100)
        multiplied = ledger.multiply_constant("mul", "mul", total, 3.25, 1000)
        self.assertEqual(first.fixed_value, 123)
        self.assertEqual(first.source_chain, ("first",))
        self.assertGreaterEqual(total.error_upper_bound, first.error_upper_bound + second.error_upper_bound)
        self.assertGreaterEqual(
            multiplied.error_upper_bound,
            total.error_upper_bound * Decimal("3.25"),
        )
        operations = Counter(entry.operation for entry in ledger.entries)
        self.assertEqual(operations["quantize"], 2)
        self.assertEqual(operations["add"], 1)
        self.assertEqual(operations["mul"], 1)
        for entry in ledger.as_dicts():
            self.assertIn(entry["rounding"], ("up", "down", "none"))
            self.assertTrue(entry["source_chain"])
            self.assertIsInstance(entry["raw_float"], float)

    def test_status_contains_complete_error_spectrum_and_sensitivity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            db_path = temp_path / "state.db"
            self.assertEqual(
                cli("load", str(ROOT / "samples" / "default"), "--db", str(db_path)).returncode,
                0,
            )
            self.assertEqual(cli("run", "--db", str(db_path)).returncode, 0)
            status = json.loads(cli("status", "--db", str(db_path)).stdout)
            self.assertTrue(status["complete"])
            entries = [
                entry
                for window in status["windows"]
                for entry in window["error_spectrum"]
            ]
            self.assertGreater(len(entries), 0)
            operations = {entry["operation"] for entry in entries}
            self.assertEqual(operations, {"quantize", "add", "mul"})
            self.assertTrue(
                any(window["precision_sensitive"] for window in status["windows"])
            )
            sensitive_events = [
                event
                for window in status["windows"]
                for event in window["precision_events"]
            ]
            self.assertTrue(sensitive_events)
            self.assertTrue(all(event["source_chain"] for event in sensitive_events))
            for batch in [
                batch for window in status["windows"] for batch in window["batches"]
            ]:
                self.assertLessEqual(
                    Decimal(batch["metrics"]["error_bound_total"]),
                    Decimal(str(batch["metrics"]["error_budget"])),
                )
                for metric in ("compute", "memory", "duration", "queue_delay"):
                    quantity = batch["metrics"][metric]
                    self.assertIn("scale", quantity)
                    self.assertIn("fixed_value", quantity)
                    self.assertIn("error_upper_bound", quantity)
if __name__ == "__main__":
    unittest.main()
