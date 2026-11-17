import unittest

from ranker.fixedpoint import (
    clip_int, quantize_levels, round_div, scale_int, to_fixed,
    ROUND_FLOOR, ROUND_HALF_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP,
)


class FixedPointTest(unittest.TestCase):
    def test_round_div_modes(self):
        self.assertEqual(round_div(3, 2, ROUND_HALF_EVEN), (2, 1))
        self.assertEqual(round_div(4, 2, ROUND_HALF_EVEN), (2, 0))
        self.assertEqual(round_div(-3, 2, ROUND_HALF_EVEN), (-2, -1))
        self.assertEqual(round_div(3, 2, ROUND_HALF_UP), (2, 1))
        self.assertEqual(round_div(-3, 2, ROUND_HALF_UP), (-2, -1))
        self.assertEqual(round_div(3, 2, ROUND_HALF_DOWN), (1, 1))
        self.assertEqual(round_div(-3, 2, ROUND_HALF_DOWN), (-1, -1))
        self.assertEqual(round_div(3, 2, ROUND_FLOOR), (1, 1))
        self.assertEqual(round_div(-3, 2, ROUND_FLOOR), (-2, 1))

    def test_to_fixed_and_residual(self):
        q, residual = to_fixed(0.5, 3, ROUND_HALF_EVEN)
        self.assertEqual(q, 4)
        self.assertEqual(residual, 0.0)
        q2, residual2 = to_fixed(0.1, 3, ROUND_FLOOR)
        self.assertEqual(q2, 0)
        self.assertGreater(residual2, 0.0)

    def test_scale_int(self):
        self.assertEqual(scale_int(7, 3, 1, ROUND_HALF_EVEN), (2, 3))
        self.assertEqual(scale_int(2, 1, 3), (8, 0))

    def test_clip(self):
        self.assertEqual(clip_int(5, 0, 3), (3, True, -2))
        self.assertEqual(clip_int(1, 0, 3), (1, False, 0))

    def test_quantize_levels_changes_integer_set(self):
        codes = [quantize_levels(x, 5, 3)[0] for x in range(9)]
        self.assertEqual(len(set(codes)), 5)
        self.assertEqual(min(codes), 0)
        self.assertEqual(max(codes), 8)

    def test_higher_bits_keep_more_precision(self):
        low = to_fixed(1 / 3, 2, ROUND_HALF_EVEN)[0]
        high = to_fixed(1 / 3, 4, ROUND_HALF_EVEN)[0]
        self.assertNotEqual(low / 4, high / 16)
        self.assertLess(abs(high / 16 - 1 / 3), abs(low / 4 - 1 / 3))


if __name__ == "__main__":
    unittest.main()
