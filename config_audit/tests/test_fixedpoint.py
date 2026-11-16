import unittest
from decimal import Decimal

from config_audit.fixedpoint import Grid, quantize, quantize_coefficient
from config_audit.errors import GridError


class FixedPointTests(unittest.TestCase):
    def test_integer_grid_half_even(self):
        grid = Grid.create(2)
        rec = quantize(Decimal("0.125"), grid, "x")
        # 0.125 * 100 = 12.5 -> half-even -> 12
        self.assertEqual(rec.quantized, 12)
        self.assertEqual(rec.down_error, 0)
        self.assertEqual(rec.up_error, 1)
        self.assertEqual(rec.rounding, "HALF_EVEN")

    def test_half_even_rounds_to_even(self):
        grid = Grid.create(0)
        self.assertEqual(quantize(Decimal("2.5"), grid, "x").quantized, 2)
        self.assertEqual(quantize(Decimal("3.5"), grid, "x").quantized, 4)

    def test_exact_integer_has_zero_bounds(self):
        grid = Grid.create(3)
        rec = quantize(Decimal("7"), grid, "x")
        self.assertEqual((rec.quantized, rec.down_error, rec.up_error),
                         (7000, 0, 0))

    def test_deterministic_repeat(self):
        grid = Grid.create(4)
        a = quantize(Decimal("0.1"), grid, "x")
        b = quantize(Decimal("0.1"), grid, "x")
        self.assertEqual(a, b)

    def test_coefficient_is_not_scaled(self):
        rec = quantize_coefficient(Decimal("2.6"))
        self.assertEqual(rec.quantized, 3)
        # 2.6 -> 3 rounds upward, so the upward error is one integer step.
        self.assertEqual(rec.down_error, 0)
        self.assertEqual(rec.up_error, 1)
        rec_down = quantize_coefficient(Decimal("2.4"))
        self.assertEqual(rec_down.quantized, 2)
        self.assertEqual(rec_down.down_error, 1)
        self.assertEqual(rec_down.up_error, 0)

    def test_invalid_grid_rejected(self):
        with self.assertRaises(GridError):
            Grid.create(-1)
        with self.assertRaises(GridError):
            Grid.create(True)


if __name__ == "__main__":
    unittest.main()
