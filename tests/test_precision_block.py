from __future__ import annotations

import unittest

import numpy as np

from ragdiag.canonical import quantize_float, stable_float_array
from ragdiag.errors import E_BLOCK_MISMATCH, WorkbenchError
from ragdiag.runtime import _verify_block_equivalence


class PrecisionBlockTests(unittest.TestCase):
    def test_rounding_modes_and_subnormal_protection(self) -> None:
        self.assertEqual(
            quantize_float(1.234567895, 8, "half_even", True), 1.2345679
        )
        self.assertEqual(
            quantize_float(1.234567895, 8, "half_up", True), 1.2345679
        )
        self.assertEqual(
            quantize_float(1.234567891, 8, "toward_zero", True), 1.23456789
        )
        tiny = np.nextafter(0.0, 1.0)
        self.assertEqual(quantize_float(tiny, 324, "half_even", True), 0.0)
        self.assertGreater(
            quantize_float(tiny, 324, "half_even", False), 0.0
        )
        array = stable_float_array(
            [0.1, 0.2, 0.3],
            {
                "score_decimals": 8,
                "rounding_mode": "half_even",
                "subnormal_protection": True,
            },
        )
        self.assertEqual(array.dtype, np.float64)
        self.assertTrue(array.flags.c_contiguous)

    def test_block_and_full_payload_are_identical(self) -> None:
        artifacts = [
            {"unit_id": "u1", "numeric_values": [0.1, 0.2, 0.3]},
            {"unit_id": "u2", "numeric_values": [1.0, 2.0]},
        ]
        precision = {
            "score_decimals": 8,
            "rounding_mode": "half_even",
            "subnormal_protection": True,
        }
        result = _verify_block_equivalence(
            artifacts, [dict(item) for item in artifacts], precision
        )
        self.assertEqual(result["status"], "identical")
        self.assertEqual(result["element_count"], 5)

    def test_mismatch_reports_first_position(self) -> None:
        block = [
            {"unit_id": "u1", "numeric_values": [0.1, 0.2]},
        ]
        full = [
            {"unit_id": "u1", "numeric_values": [0.1, 0.3]},
        ]
        precision = {
            "score_decimals": 8,
            "rounding_mode": "half_even",
            "subnormal_protection": True,
        }
        with self.assertRaises(WorkbenchError) as caught:
            _verify_block_equivalence(block, full, precision)
        self.assertEqual(caught.exception.code, E_BLOCK_MISMATCH)
        self.assertEqual(
            caught.exception.details["first_deviation"]["element_index"], 1
        )


if __name__ == "__main__":
    unittest.main()

