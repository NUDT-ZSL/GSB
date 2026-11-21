from __future__ import annotations

import unittest
from decimal import Decimal
from fractions import Fraction

from capacity_engine.ledger import LedgerBuilder, round_half_up_fraction


class FixedPointLedgerTests(unittest.TestCase):
    def test_rounding_directions(self) -> None:
        value, discarded, direction = round_half_up_fraction(Fraction(5, 2))
        self.assertEqual(value, 3)
        self.assertEqual(discarded, Fraction(-1, 2))
        self.assertEqual(direction, "up")
        value, discarded, direction = round_half_up_fraction(Fraction(1, 3))
        self.assertEqual(value, 0)
        self.assertEqual(discarded, Fraction(1, 3))
        self.assertEqual(direction, "down")
        value, discarded, direction = round_half_up_fraction(Fraction(4, 2))
        self.assertEqual(value, 2)
        self.assertEqual(discarded, 0)
        self.assertEqual(direction, "exact")

    def test_ratio_ledger_records_remainder_and_error(self) -> None:
        ledger = LedgerBuilder("ratio")
        entry = ledger.add_ratio("success_rate", 1, 3, 100, "unit-test")
        self.assertEqual(entry.result_scaled, 33)
        self.assertEqual(entry.rounding_direction, "down")
        self.assertEqual(entry.discarded, Fraction(1, 3))
        self.assertEqual(entry.conversion_error, Fraction(1, 3))
        self.assertEqual(entry.local_seq, 0)
        self.assertEqual(entry.operation, "divide_round_half_up")

    def test_p95_linear_interpolation(self) -> None:
        ledger = LedgerBuilder("p95")
        samples = [(f"r{i}", Decimal(i)) for i in range(1, 21)]
        entry = ledger.add_p95("p95", samples, 1, "unit-test")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.result_scaled, 19)
        self.assertEqual(entry.details["lower_index"], 18)
        self.assertEqual(entry.details["upper_index"], 19)
        self.assertEqual(entry.details["position"], "361/20")
        self.assertGreater(entry.cumulative_error, 0)
        self.assertEqual(len([item for item in ledger.entries if item.kind == "latency_sample"]), 20)

    def test_repeated_build_is_identical(self) -> None:
        samples = [(f"r{i}", Decimal(str(10 + i / 10))) for i in range(20)]
        first = LedgerBuilder("same")
        second = LedgerBuilder("same")
        a = first.add_p95("p95", samples, 10, "unit-test")
        b = second.add_p95("p95", samples, 10, "unit-test")
        self.assertEqual(a.as_dict(), b.as_dict())
        self.assertEqual([item.as_dict() for item in first.entries], [item.as_dict() for item in second.entries])


if __name__ == "__main__":
    unittest.main()

