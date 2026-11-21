from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from dimdiag.engine import DiagnosticEngine
from dimdiag.precision import PrecisionOps, PrecisionSpec
from dimdiag.errors import (
    E_CONFIG_INVALID,
    E_INTERMEDIATE_DIM,
    E_TOLERANCE_EXCEEDED,
    BusinessError,
)

from .helpers import base_config, prepare_workspace


class CoreAndErrorTests(unittest.TestCase):
    def test_fixed_one_hot_uses_scaled_unit(self):
        ops = PrecisionOps(PrecisionSpec("fixed", fixed_scale=1024))
        values = [["a"], ["b"], ["a"]]
        block, names = ops.one_hot(
            np.asarray(values),
            ["cat"],
            {"cat": ["a", "b"]},
        )
        self.assertEqual(names, ["cat=a", "cat=b"])
        self.assertEqual(int(block.max()), 1024)
        self.assertEqual(ops.to_float(block)[0].tolist(), [1.0, 0.0])

    def test_standardization_can_be_disabled(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            config["standardize"] = False
            engine = DiagnosticEngine(root, base_dir=config_path.parent)
            outcome = engine.run(config, base_dir=config_path.parent)
            self.assertTrue(outcome.selfcheck["overall_passed"])
            self.assertEqual(
                outcome.snapshot["sections"]["standardization"]["content"][
                    "standardize"
                ],
                False,
            )

    def test_intermediate_dimension_below_target(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            config["method"] = "pca_random_projection"
            config["target_dim"] = 5
            config["two_stage"]["intermediate_dim"] = 4
            engine = DiagnosticEngine(root, base_dir=config_path.parent)
            with self.assertRaises(BusinessError) as raised:
                engine.run(config, base_dir=config_path.parent)
            self.assertEqual(raised.exception.code, E_INTERMEDIATE_DIM)
            self.assertEqual(
                raised.exception.details["field"],
                "two_stage.intermediate_dim",
            )

    def test_intermediate_dimension_above_features(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            config["method"] = "pca_random_projection"
            config["two_stage"]["intermediate_dim"] = 999
            engine = DiagnosticEngine(root, base_dir=config_path.parent)
            with self.assertRaises(BusinessError) as raised:
                engine.run(config, base_dir=config_path.parent)
            self.assertEqual(raised.exception.code, E_INTERMEDIATE_DIM)
            self.assertEqual(
                raised.exception.details["field"],
                "two_stage.intermediate_dim",
            )

    def test_error_spectrum_has_required_levels_and_attribution(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            config["method"] = "pca_random_projection"
            config["precision"].update(
                {"mode": "fixed", "fixed_scale": 8, "rounding": "half_even"}
            )
            for key in config["error_tolerance"]:
                config["error_tolerance"][key] = 1000.0
            outcome = DiagnosticEngine(root, base_dir=config_path.parent).run(
                config, base_dir=config_path.parent, commit=False
            )
            stages = {
                stage["stage"]: stage for stage in outcome.error_spectrum["stages"]
            }
            self.assertEqual(
                set(stages),
                {
                    "standardization",
                    "model_construction",
                    "coordinates",
                    "two_stage_intermediate",
                },
            )
            self.assertIn(
                outcome.error_spectrum["dominant_source"],
                {
                    "standardization",
                    "projection_matrix_construction",
                    "coordinate_computation",
                    "two_stage_combination",
                },
            )
            for stage in stages.values():
                self.assertIn("absolute_error", stage)
                self.assertIn("relative_error", stage)
                self.assertIn("error_interval", stage)
                self.assertIn("error_amplification_ratio", stage)
                self.assertTrue(stage["top_elements"])

    def test_tolerance_violation_reports_elements(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            config["precision"]["mode"] = "float32"
            for key in config["error_tolerance"]:
                config["error_tolerance"][key] = 1e-15
            engine = DiagnosticEngine(root, base_dir=config_path.parent)
            with self.assertRaises(BusinessError) as raised:
                engine.run(config, base_dir=config_path.parent)
            error = raised.exception
            self.assertEqual(error.code, E_TOLERANCE_EXCEEDED)
            self.assertGreater(error.details["violation_count"], 0)
            violation = error.details["violations"][0]
            self.assertIn("stage", violation)
            self.assertIn("reference", violation)
            self.assertIn("observed", violation)
            self.assertIn("abs_error", violation)
            self.assertGreater(violation["abs_error"], 0)

    def test_invalid_precision_config(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            config["precision"]["rounding"] = "nearest_magic"
            with self.assertRaises(BusinessError) as raised:
                DiagnosticEngine(root, base_dir=config_path.parent).run(
                    config, base_dir=config_path.parent
                )
            self.assertEqual(raised.exception.code, E_CONFIG_INVALID)


if __name__ == "__main__":
    unittest.main()

