from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quantlab.errors import (
    BusinessError,
    E_RESOURCE_LIMIT,
    E_TOLERANCE_EXCEEDED,
    E_UNATTRIBUTED,
    attribute_stage,
)
from quantlab.pipeline import Workbench

from tests.helpers import make_config


class DiagnosticsAndBudgetTests(unittest.TestCase):
    def test_error_spectrum_intervals_and_fixed_point_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = Workbench(make_config(Path(temp))).run(clean=True)
            diagnostics = result["diagnostics"]
            levels = {
                item["level"]
                for layer in diagnostics["error_spectrum"]
                for item in layer["levels"]
            }
            self.assertEqual(
                levels,
                {
                    "quantization_rounding",
                    "group_scaling",
                    "block_accumulation",
                    "layer_cascade",
                },
            )
            self.assertIn(
                diagnostics["dominant_source"],
                {
                    "quantization_rounding",
                    "group_scaling",
                    "block_accumulation",
                    "layer_cascade",
                },
            )
            self.assertTrue(diagnostics["intervals_legal"])
            self.assertTrue(diagnostics["fixed_point_all_match"])
            self.assertTrue(result["consistency"]["ok"])
            for layer in diagnostics["error_spectrum"]:
                for item in layer["levels"]:
                    self.assertLessEqual(item["interval"][0], item["interval"][1])
                    self.assertGreaterEqual(item["amplification_ratio"], 0.0)

    def test_tolerance_violation_reports_layer_and_first_element(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), tolerance_abs=1e-12)
            with self.assertRaises(BusinessError) as captured:
                Workbench(config).run(clean=True)
            error = captured.exception
            self.assertEqual(error.code, E_TOLERANCE_EXCEEDED)
            self.assertIsNotNone(error.layer_id)
            self.assertIsNotNone(error.layer_index)
            self.assertIsInstance(error.details["first_element_index"], list)
            self.assertGreaterEqual(len(error.details["first_element_index"]), 1)
            self.assertEqual(error.stage, "diagnostics")

    def test_stage_layer_budget_returns_resource_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), max_layers_per_stage=1)
            with self.assertRaises(BusinessError) as captured:
                Workbench(config).run(clean=True)
            self.assertEqual(captured.exception.code, E_RESOURCE_LIMIT)

    def test_weight_memory_budget_returns_resource_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), max_resident_weight_bytes=16)
            with self.assertRaises(BusinessError) as captured:
                Workbench(config).run(clean=True)
            self.assertEqual(captured.exception.code, E_RESOURCE_LIMIT)
            self.assertEqual(captured.exception.stage, "quantization")
            self.assertIsNotNone(captured.exception.layer_id)

    def test_smaller_block_is_used_and_degradation_is_marked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), max_block_elements=3)
            result = Workbench(config).run(clean=True)
            categories = result["budget"]["degradation_categories"]
            self.assertIn("block_size", categories)
            self.assertTrue(result["budget"]["degradations"])
            self.assertTrue(result["consistency"]["ok"])

    def test_calibration_budget_subsamples_and_marks_degradation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), max_calibration_comparisons=1000)
            result = Workbench(config).run(clean=True)
            self.assertIn(
                "calibration_subsample",
                result["budget"]["degradation_categories"],
            )
            self.assertTrue(
                all(
                    item["comparison_count"] <= 1000
                    for item in result["calibration"]["layers"]
                )
            )

    def test_unattributed_exception_has_stable_code(self) -> None:
        error = attribute_stage("inference", ValueError("boom"))
        self.assertEqual(error.code, E_UNATTRIBUTED)
        self.assertEqual(error.stage, "inference")


if __name__ == "__main__":
    unittest.main()
