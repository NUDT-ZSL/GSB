from __future__ import annotations

import unittest

from schedkernel.precision import add, constant, convert_fixed, multiply


class PrecisionTests(unittest.TestCase):
    def test_non_decimal_scale_is_supported(self) -> None:
        fixed = convert_fixed(
            constant(1.2345, "value"),
            scale=3,
            rounding_mode="half_even",
            label="ternary_scale",
            error_budget=1.0,
        )
        self.assertEqual(fixed.fixed, 4)
        self.assertEqual(fixed.rounding_direction, "up")
        self.assertAlmostEqual(fixed.as_float, 4 / 3)
        self.assertGreater(fixed.accumulated_error_bound, 0.0)
        self.assertTrue(fixed.within_budget)

    def test_add_and_multiply_accumulate_bounds(self) -> None:
        left = add(constant(0.1, "a"), constant(0.2, "b"), "a+b")
        result = multiply(left, constant(3.0, "c"), "(a+b)*c")
        self.assertGreater(left.error, 0.0)
        self.assertGreater(result.error, left.error)
        self.assertEqual(result.value, (0.1 + 0.2) * 3.0)


if __name__ == "__main__":
    unittest.main()

