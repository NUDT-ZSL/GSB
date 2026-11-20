from __future__ import annotations

import unittest

from qos_pipeline.engine import Engine
from qos_pipeline.fixed import (
    FIXED_SCALE,
    TOKEN_DENOMINATOR,
    add_refill,
    convert_rate,
    fraction_text,
)
from qos_pipeline.sample import load_sample

from .helpers import ROOT


class FixedPointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sample = load_sample(ROOT / "samples" / "manifest.json")

    def test_rate_conversion_is_bit_stable_and_records_inputs(self) -> None:
        first = convert_rate(12_000_001, 5, "access", "committed_rate")
        second = convert_rate(12_000_001, 5, "access", "committed_rate")
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(first.scale, FIXED_SCALE)
        self.assertEqual(first.increment_units, 7_500_000)
        self.assertEqual(first.rate_remainder, 5_000)
        self.assertEqual(first.rounding, "floor")
        self.assertEqual(first.raw_float, "12000001.0")
        self.assertEqual(first.denominator, TOKEN_DENOMINATOR)

    def test_saved_remainder_keeps_long_run_to_one_unit(self) -> None:
        conversion = convert_rate(12_000_001, 5, "access", "committed_rate")
        tokens = 0
        remainder = 0
        capacity = 10**18
        for _ in range(10_000):
            tokens, remainder, _, _ = add_refill(
                tokens,
                capacity,
                conversion.increment_units,
                conversion.rate_remainder,
                remainder,
            )
        exact = conversion.exact_increment * 10_000
        self.assertLessEqual(abs(tokens - exact), 1)
        self.assertLess(remainder, TOKEN_DENOMINATOR)

    def test_engine_state_contains_per_bucket_remainders(self) -> None:
        state = Engine(self.sample).initial_state()
        bucket = state["buckets"]["access"]
        self.assertIn("committed_remainder", bucket)
        self.assertIn("peak_remainder", bucket)
        self.assertEqual(bucket["committed_remainder"], 0)
        self.assertEqual(bucket["peak_remainder"], 0)

    def test_fraction_text_rounds_negative_values_symmetrically(self) -> None:
        from fractions import Fraction

        self.assertEqual(fraction_text(Fraction(-2, 3), 6), "-0.666667")
        self.assertEqual(fraction_text(Fraction(2, 3), 6), "0.666667")


if __name__ == "__main__":
    unittest.main()

