from __future__ import annotations

import unittest
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from leasecoord.numeric import (
    exact_product_fixed,
    fixed_from_fraction,
    fixed_from_input,
    fraction,
    fraction_text,
)

from .helpers import load_sample


class FixedPointTests(unittest.TestCase):
    def test_repeated_conversion_is_bit_identical(self) -> None:
        first = fixed_from_input("1.0000004", 1_000_000, label="lease_ms").as_dict()
        second = fixed_from_input("1.0000004", 1_000_000, label="lease_ms").as_dict()
        self.assertEqual(first, second)
        self.assertEqual(first["units"], 1_000_000)
        self.assertEqual(first["residual_num"], 2)
        self.assertEqual(first["residual_den"], 5)
        self.assertEqual(first["scale"], 1_000_000)
        self.assertEqual(first["rounding"], "floor")
        self.assertLess(first["raw_float"], 1.000001)
        self.assertEqual(
            first["retained_residual_comparison_error_upper_bound_units"], "0"
        )
        self.assertLessEqual(abs(Fraction(first["residual_num"], first["residual_den"])), 1)

    def test_safety_product_keeps_per_value_residual(self) -> None:
        ratio = Fraction(50001, 1_000_000)
        product = exact_product_fixed(1000, ratio, 1_000_000)
        self.assertEqual(product.units, 50_001_000)
        self.assertEqual(product.residual, Fraction(0, 1))
        expected = Fraction(1000) * ratio
        self.assertEqual(product.value, expected)
        self.assertEqual(fraction_text(expected), "50.001")

    def test_fraction_text_is_canonical(self) -> None:
        self.assertEqual(fraction_text(Fraction(1, 8)), "0.125")
        self.assertEqual(fraction_text(Fraction(2, 10000)), "0.0002")
        self.assertEqual(fraction_text(Fraction(-2, 10000)), "-0.0002")
        self.assertEqual(fraction_text(Fraction(0, 1)), "0")

    def test_status_exposes_scale_residual_and_rounding(self) -> None:
        with TemporaryDirectory() as temp:
            engine, _, _ = load_sample(Path(temp) / "ws")
            engine.elect(clock="0")
            engine.renew(node="alpha", session_epoch=1, clock="100")
            engine.renew(node="charlie", session_epoch=3, clock="100.0002")
            status = engine.status(clock="100.0002")
            alpha = next(item for item in status["nodes"] if item["name"] == "alpha")
            self.assertEqual(alpha["lease"]["remaining_ms"], "999.9998")
            self.assertEqual(alpha["lease"]["remaining_units"], 999_999_800)
            self.assertIn("remaining_residual_num", alpha["lease"])
            self.assertIn("remaining_residual_den", alpha["lease"])
            self.assertEqual(alpha["lease"]["remaining_fixed"]["scale"], 1_000_000)
            self.assertEqual(alpha["lease"]["remaining_fixed"]["rounding"], "floor")
            self.assertEqual(
                alpha["lease"]["remaining_fixed"][
                    "retained_residual_comparison_error_upper_bound_units"
                ],
                "0",
            )

    def test_exact_fraction_input_is_deterministic(self) -> None:
        value = fixed_from_fraction(
            fraction("0.05000000000000001"),
            1_000_000,
            label="ratio",
            raw_value="0.05000000000000001",
            raw_float=0.05,
        )
        self.assertEqual(value.units, 50_000)
        self.assertEqual(value.residual, Fraction(1, 100_000_000_000))


if __name__ == "__main__":
    unittest.main()
