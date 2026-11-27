from __future__ import annotations

import unittest

from flight_compile_cache.tensors import Tensor, compare_outputs, make_input_tensor


class TensorTests(unittest.TestCase):
    def test_input_generation_is_deterministic(self) -> None:
        first = make_input_tensor((3, 4), {"seed": 1})
        second = make_input_tensor((3, 4), {"seed": 1})
        third = make_input_tensor((3, 4), {"seed": 2})
        self.assertEqual(first.data, second.data)
        self.assertNotEqual(first.data, third.data)

    def test_comparison_reports_element_position_and_max_error(self) -> None:
        left = Tensor((2, 2), [1.0, 2.0, 3.0, 4.0])
        right = Tensor((2, 2), [1.0, 2.0, 3.5, 4.0])
        report = compare_outputs({"score": left}, {"score": right}, 0.1)
        self.assertFalse(report["consistent"])
        self.assertAlmostEqual(report["max_abs_error"], 0.5)
        self.assertEqual(report["max_location"]["index"], [1, 0])
        self.assertTrue(report["violations"])
        self.assertEqual(report["violations"][0]["index"], [1, 0])

    def test_comparison_within_tolerance(self) -> None:
        left = Tensor((1, 1), [1.0])
        right = Tensor((1, 1), [1.0000001])
        report = compare_outputs({"x": left}, {"x": right}, 1e-6)
        self.assertTrue(report["consistent"])


if __name__ == "__main__":
    unittest.main()
