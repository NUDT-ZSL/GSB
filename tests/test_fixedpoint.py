from __future__ import annotations

import unittest
from decimal import Decimal

from elastic_control.fixedpoint import FixedPointBook, replay_ledger


class FixedPointTests(unittest.TestCase):
    def test_residual_is_carried_into_the_next_conversion(self) -> None:
        book = FixedPointBook()
        first = book.convert(
            "m", Decimal("1.23456"), 10, operation="aggregate"
        )
        self.assertEqual(first, 12)
        self.assertEqual(book.ledgers["m"].residual, Decimal("0.03456"))
        second = book.convert("m", Decimal("1.96544"), 10, operation="target")
        self.assertEqual(second, 20)
        self.assertEqual(book.ledgers["m"].residual, Decimal("0"))
        residual, cumulative, scale, next_step = replay_ledger(
            "m", [entry.to_dict() for entry in book.ledgers["m"].entries]
        )
        self.assertEqual(residual, Decimal("0"))
        self.assertEqual(cumulative, book.ledgers["m"].cumulative_error_bound)
        self.assertEqual(scale, 10)
        self.assertEqual(next_step, 3)

    def test_product_and_ratio_error_are_accumulated(self) -> None:
        book = FixedPointBook()
        book.convert("m", Decimal("1.25"), 1000, operation="aggregate")
        book.convert("m", Decimal("0.5"), 1000, operation="target")
        product = book.record_product(
            "m",
            left_name="current",
            left=3,
            left_error=Decimal(0),
            right_name="aggregate",
            right=1250,
            right_error=Decimal("0.001"),
            scale=1000,
            operation="ratio_numerator",
        )
        self.assertEqual(product, 3750)
        book.record_operation_error(
            "m",
            operation="ratio",
            error_increment=Decimal("0.002"),
            scale=1000,
        )
        self.assertGreater(book.ledgers["m"].cumulative_error_bound, Decimal(0))
        replay_ledger("m", [entry.to_dict() for entry in book.ledgers["m"].entries])

    def test_fixed_decimal_divergence_reports_step_and_values(self) -> None:
        book = FixedPointBook()
        units = book.convert("m", Decimal("0.1234"), 10, operation="aggregate")
        divergence = book.compare(
            "m",
            scale=10,
            fixed_units=units,
            decimal_value=Decimal("0.1234"),
            tolerance=Decimal("0.0001"),
            operation="aggregate",
            step_id="m:aggregate",
        )
        self.assertIsNotNone(divergence)
        assert divergence is not None
        self.assertEqual(divergence.metric, "m")
        self.assertEqual(divergence.fixed_value, Decimal("0.1"))
        self.assertEqual(divergence.decimal_value, Decimal("0.1234"))
        self.assertTrue(divergence.step_id)


if __name__ == "__main__":
    unittest.main()
