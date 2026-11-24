from __future__ import annotations

import math
import unittest

import numpy as np

from soundfield.config import FP_SCALE
from soundfield.fixedpoint import (
    chain_mul_q,
    dequantize,
    fixed_convolve,
    mul_q,
    quantize,
    quantize_array,
    to_int16,
)


class FixedPointTest(unittest.TestCase):
    def test_quantize_roundtrip(self) -> None:
        for value in (0.0, 0.1, -0.25, 0.999, 1.0 / 3.0):
            q = quantize(value)
            self.assertIsInstance(q, int)
            self.assertLessEqual(abs(dequantize(q) - value), 0.5 / FP_SCALE + 1e-18)

    def test_mul_q_stays_on_scale(self) -> None:
        a, b = quantize(0.7), quantize(0.13)
        product = mul_q(a, b)
        self.assertLessEqual(abs(dequantize(product) - 0.7 * 0.13), 1.0 / FP_SCALE)

    def test_chain_mul_deterministic(self) -> None:
        values = [quantize(v) for v in (0.12, 0.35, 0.88, 0.42)]
        first = chain_mul_q(values)
        second = chain_mul_q(list(reversed(values)))
        # 定点逐步舍入不满足交换律，体现反射链顺序依赖；但同序确定。
        self.assertEqual(chain_mul_q(values), first)
        self.assertIsInstance(first, int)
        self.assertNotEqual(first, second)

    def test_convolve_scale_is_q24(self) -> None:
        x = quantize_array(np.asarray([1.0, 0.5, 0.0]))
        h = quantize_array(np.asarray([1.0, -0.25]))
        y = fixed_convolve(x, h)
        expected = np.convolve(np.asarray([1.0, 0.5, 0.0]), np.asarray([1.0, -0.25]))
        for got, want in zip(y.tolist(), expected.tolist()):
            self.assertLessEqual(abs(int(got) / FP_SCALE - want), 2.0 / FP_SCALE)

    def test_to_int16_saturates(self) -> None:
        huge = np.asarray([FP_SCALE * 10, -FP_SCALE * 10], dtype=object)
        pcm = to_int16(huge)
        self.assertEqual(pcm.tolist(), [32767, -32768])

    def test_non_finite_rejected(self) -> None:
        with self.assertRaises(OverflowError):
            quantize(math.nan)


if __name__ == "__main__":
    unittest.main()
