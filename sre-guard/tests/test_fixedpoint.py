import unittest

from sre_guard.errors import E_GRID, GovernanceError
from sre_guard.fixedpoint import (
    Grid,
    compare_percent,
    compose_chain_probability,
    div_fixed,
    format_fixed,
    mul_fixed,
    quantize,
)


class FixedPointTests(unittest.TestCase):
    def setUp(self):
        self.grid = Grid.from_exponent(4)  # 1 ulp = 0.0001

    def test_quantize_floor_direction_and_remainder(self):
        result = quantize(1, 3, self.grid)  # 0.3333...
        self.assertEqual(result.value, 3333)
        self.assertEqual(result.remainder, 1)
        self.assertLess(result.remainder, 3)  # 余数是分子单位，误差 < 1 ulp
        self.assertEqual(result.error_ulps, 1)

    def test_mul_and_div_truncation_bound_one_ulp(self):
        value, remainder = mul_fixed(9950, 9000, self.grid)  # 0.995 * 0.9
        self.assertEqual(value, 8955)  # floor(0.8955)
        self.assertGreaterEqual(remainder, 0)
        self.assertLess(remainder, self.grid.scale)  # 截断余数 < 1 格
        quotient, q_remainder = div_fixed(7, 8, self.grid)
        self.assertEqual(quotient, 8750)
        self.assertEqual(q_remainder, 0)

    def test_compare_percent_exact_cross_multiply(self):
        # 3/6 = 50%，阈值 50 应触发；阈值 51 不触发
        self.assertTrue(compare_percent(3, 6, 50))
        self.assertFalse(compare_percent(3, 6, 51))

    def test_format_no_float(self):
        self.assertEqual(format_fixed(12345, self.grid), "1.2345")
        self.assertEqual(format_fixed(5, self.grid), "0.0005")

    def test_chain_error_growth_per_level(self):
        steps = [("a", 9950, 0), ("b", 9000, 1), ("c", 7000, 0)]
        value, trace = compose_chain_probability(steps, self.grid)
        self.assertEqual(len(trace.levels), 3)
        # 每级乘法 1 ulp + 第二级输入量化 1 ulp = 4 ulp
        self.assertEqual(trace.total_error_ulp, 4)
        self.assertLessEqual(value, self.grid.scale)
        self.assertEqual(trace.levels[1].input_quant_ulp, 1)

    def test_invalid_grid_exponent(self):
        with self.assertRaises(GovernanceError) as ctx:
            Grid.from_exponent(-1)
        self.assertEqual(ctx.exception.code, E_GRID)


if __name__ == "__main__":
    unittest.main()
