from __future__ import annotations

import unittest

from numeric_workbench.precision import Quantizer, round_ratio_int


class PrecisionSemanticsTests(unittest.TestCase):
    def test_round_ratio_modes_for_positive_and_negative_values(self):
        expected = {
            "nearest_even": [2, -2, 0, 0],
            "half_away_from_zero": [2, -2, 1, -1],
            "toward_zero": [1, -1, 0, 0],
            "floor": [1, -2, 0, -1],
            "ceil": [2, -1, 1, 0],
        }
        for mode, values in expected.items():
            actual = [
                int(round_ratio_int(value, 2, mode))
                for value in (3, -3, 1, -1)
            ]
            self.assertEqual(actual, values)

    def test_quantizer_round_trip_is_exact(self):
        quantizer = Quantizer(1024, "nearest_even")
        encoded = quantizer.quantize([1.25, -0.5, 0.125])
        self.assertEqual(encoded.tolist(), [1280, -512, 128])
        self.assertEqual(quantizer.dequantize(encoded).tolist(), [1.25, -0.5, 0.125])


if __name__ == "__main__":
    unittest.main()
