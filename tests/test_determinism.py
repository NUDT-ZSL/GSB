from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dimdiag.data import write_demo_sample
from dimdiag.engine import DiagnosticEngine

from .helpers import base_config, prepare_workspace


class DeterminismTests(unittest.TestCase):
    def test_generated_sample_is_byte_identical(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            left_path = Path(left)
            right_path = Path(right)
            write_demo_sample(left_path)
            write_demo_sample(right_path)
            self.assertEqual(
                (left_path / "data" / "demo.csv").read_bytes(),
                (right_path / "data" / "demo.csv").read_bytes(),
            )
            self.assertEqual(
                (left_path / "data" / "demo.meta.json").read_bytes(),
                (right_path / "data" / "demo.meta.json").read_bytes(),
            )
            self.assertEqual(
                (left_path / "configs" / "demo.json").read_bytes(),
                (right_path / "configs" / "demo.json").read_bytes(),
            )

    def test_pca_model_and_coordinates_are_byte_reproducible(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            left_root = Path(left)
            right_root = Path(right)
            left_config_path = prepare_workspace(left_root)
            right_config_path = prepare_workspace(right_root)
            common = base_config(left_config_path)
            left_out = DiagnosticEngine(
                left_root, base_dir=left_config_path.parent
            ).run(common, base_dir=left_config_path.parent)
            right_out = DiagnosticEngine(
                right_root, base_dir=right_config_path.parent
            ).run(common, base_dir=right_config_path.parent)
            self.assertEqual(
                left_out.observed.coordinate_checksum(),
                right_out.observed.coordinate_checksum(),
            )
            self.assertEqual(
                left_out.observed.model_checksum(),
                right_out.observed.model_checksum(),
            )
            self.assertEqual(
                left_out.snapshot["sections"]["coordinates"]["checksum"],
                right_out.snapshot["sections"]["coordinates"]["checksum"],
            )
            self.assertEqual(
                left_out.snapshot["sections"]["model"]["checksum"],
                right_out.snapshot["sections"]["model"]["checksum"],
            )
            self.assertEqual(
                left_out.snapshot["input"]["checksum"],
                right_out.snapshot["input"]["checksum"],
            )

    def test_random_projection_repeat_is_identical(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            config["method"] = "random_projection"
            engine = DiagnosticEngine(root, base_dir=config_path.parent)
            first = engine.run(config, base_dir=config_path.parent)
            second = engine.run(config, base_dir=config_path.parent)
            self.assertEqual(
                first.observed.coordinate_checksum(),
                second.observed.coordinate_checksum(),
            )
            self.assertEqual(
                first.observed.projection_stages[0]["matrix_checksum"],
                second.observed.projection_stages[0]["matrix_checksum"],
            )
            self.assertEqual(second.snapshot["snapshot_version"], 2)

    def test_fixed_point_repeat_is_identical(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            config["precision"].update(
                {"mode": "fixed", "fixed_scale": 65536, "rounding": "half_even"}
            )
            for key in config["error_tolerance"]:
                config["error_tolerance"][key] = 1.0
            engine = DiagnosticEngine(root, base_dir=config_path.parent)
            first = engine.run(config, base_dir=config_path.parent)
            second = engine.run(config, base_dir=config_path.parent)
            self.assertEqual(
                first.observed.coordinate_checksum(),
                second.observed.coordinate_checksum(),
            )
            self.assertEqual(first.observed.coordinates.dtype.str, "<i8")

    def test_fixed_two_stage_is_verifiable_and_within_demo_tolerance(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            config["method"] = "pca_random_projection"
            config["precision"].update(
                {"mode": "fixed", "fixed_scale": 16384, "rounding": "half_even"}
            )
            outcome = DiagnosticEngine(root, base_dir=config_path.parent).run(
                config, base_dir=config_path.parent
            )
            self.assertTrue(outcome.selfcheck["overall_passed"])
            self.assertTrue(outcome.error_spectrum["tolerance_passed"])
            self.assertEqual(
                outcome.observed.intermediate_coordinates.shape[1],
                config["two_stage"]["intermediate_dim"],
            )


if __name__ == "__main__":
    unittest.main()

