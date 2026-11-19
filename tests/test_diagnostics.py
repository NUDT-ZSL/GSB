from __future__ import annotations

import unittest

import numpy as np

from sparse_precision.api import execute_case
from sparse_precision.codes import E_TOLERANCE_EXCEEDED
from sparse_precision.diagnostics import diagnose_error_spectrum
from sparse_precision.models import OperatorConfig

from tests.helpers import metadata_for


class DiagnosticsTests(unittest.TestCase):
    def test_spectrum_fields_and_violation_details(self) -> None:
        reference = np.array([[1.0, 0.0], [1e-12, -4.0]])
        actual = np.array([[1.0 + 1e-10, 1e-12], [2e-12, -4.0]])
        spectrum = diagnose_error_spectrum(
            reference,
            actual,
            OperatorConfig(atol=0.0, rtol=0.0),
            case_id="spectrum_demo",
        )
        self.assertFalse(spectrum["passed"])
        self.assertEqual(spectrum["violation_count"], 3)
        self.assertEqual(
            set(spectrum["abs_error_quantiles"]),
            {"p0", "p50", "p90", "p99", "p100"},
        )
        self.assertIsNotNone(spectrum["max_abs_error_position"])
        self.assertIsNotNone(spectrum["max_relative_error_position"])
        self.assertGreater(spectrum["error_amplification_ratio"], 0)
        self.assertGreaterEqual(len(spectrum["worst_violations"]), 1)
        self.assertIn("allowed_error", spectrum["worst_violations"][0])
        self.assertIn("excess", spectrum["worst_violations"][0])

    def test_public_entry_returns_tolerance_business_code(self) -> None:
        config = OperatorConfig(
            atol=0.0,
            rtol=0.0,
            compensated_sum=False,
            forced_row_block=8,
            forced_accumulation_order="reverse",
            enable_self_check=False,
        )
        report = execute_case(metadata_for("spmm_vector_1"), config=config)
        self.assertEqual(report["code"], E_TOLERANCE_EXCEEDED)
        self.assertFalse(report["ok"])
        self.assertEqual(report["field"], "result")
        self.assertGreater(report["details"]["violation_count"], 0)
        self.assertGreaterEqual(len(report["details"]["worst_violations"]), 1)
        self.assertTrue(
            report["diagnostics"]["stage_attribution"]["dominant_source"]
            in {
                "sparse_index_summation",
                "cross_block_reduction",
                "row_block",
                "none",
            }
        )


if __name__ == "__main__":
    unittest.main()

