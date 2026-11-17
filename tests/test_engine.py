from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from infer_workbench.assets import load_assets
from infer_workbench.canonical import canonical_json
from infer_workbench.config import BatchConfig
from infer_workbench.engine import float_forward
from infer_workbench.session import InferenceSession


ROOT = Path(__file__).resolve().parent.parent


def _config(**overrides):
    raw = {
        "batch_size": 3,
        "sort_strategy": "sample_id",
        "precision_mode": "fixed",
        "quant_scale": 1000,
        "drift_threshold": 0.005,
    }
    raw.update(overrides)
    return BatchConfig.from_dict(raw)


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.assets = load_assets(ROOT / "data")

    def test_float_forward_is_exactly_repeatable(self) -> None:
        sample = self.assets.samples[3]
        one = float_forward(sample, self.assets.layers)
        two = float_forward(sample, self.assets.layers)
        for layer_name in self.assets.layer_names:
            self.assertEqual(
                np.asarray(one["trace"][layer_name], dtype=np.float32).tobytes(),
                np.asarray(two["trace"][layer_name], dtype=np.float32).tobytes(),
            )

    def test_same_config_reports_are_bitwise_identical(self) -> None:
        config = _config()
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            session = InferenceSession(self.assets, snapshot_dir=first_dir)
            first = session.run(config)
            second = InferenceSession(self.assets, snapshot_dir=second_dir).run(config)
            rerun = session.run(config)
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(canonical_json(first), canonical_json(rerun))

    def test_float_mode_records_zero_diagnostics(self) -> None:
        config = _config(precision_mode="float32")
        with tempfile.TemporaryDirectory() as temporary:
            report = InferenceSession(self.assets, snapshot_dir=temporary).run(config)
        for batch in report["batches"]:
            for sample in batch["samples"]:
                diagnostics = sample["diagnostics"]
                self.assertFalse(diagnostics["applicable"])
                self.assertEqual(diagnostics["output_relative_deviation"], 0.0)
                self.assertTrue(
                    all(layer["cumulative_error_bound_grid"] == 0 for layer in diagnostics["layers"])
                )

    def test_fixed_diagnostics_use_integer_grid_and_drift_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            q3_report = InferenceSession(self.assets, snapshot_dir=temporary).run(_config())
        self.assertNotIn("E_DRIFT", q3_report["business_codes"])
        for batch in q3_report["batches"]:
            for sample in batch["samples"]:
                previous = 0
                for layer in sample["diagnostics"]["layers"]:
                    self.assertIsInstance(layer["local_error_bound_grid"], int)
                    self.assertIsInstance(layer["cumulative_error_bound_grid"], int)
                    self.assertGreaterEqual(layer["local_error_bound_grid"], 0)
                    self.assertGreaterEqual(layer["cumulative_error_bound_grid"], layer["local_error_bound_grid"])
                    self.assertGreaterEqual(layer["cumulative_error_bound_grid"], previous)
                    self.assertLessEqual(
                        layer["abs_error"] * q3_report["configuration"]["quant_scale"],
                        layer["cumulative_error_bound_grid"] + 1e-6,
                    )
                    previous = layer["cumulative_error_bound_grid"]
                    self.assertIn("raw_dot_bound_grid", layer["bound_inputs"])
                    self.assertIn("derivation", q3_report["diagnostics"]["bound_method"])

        with tempfile.TemporaryDirectory() as temporary:
            q2_report = InferenceSession(self.assets, snapshot_dir=temporary).run(_config(quant_scale=100))
        self.assertIn("E_DRIFT", q2_report["business_codes"])
        drift_batches = [batch for batch in q2_report["batches"] if batch["drift"]]
        self.assertEqual(len(drift_batches), len(q2_report["batches"]))
        for batch in drift_batches:
            self.assertEqual(batch["business_codes"], ["E_DRIFT"])
            self.assertIsNotNone(batch["first_drift"])
            self.assertIn("sample_id", batch["first_drift"])
            self.assertIn("layer", batch["first_drift"])
            for sample in batch["samples"]:
                for layer in sample["diagnostics"]["layers"]:
                    self.assertLessEqual(
                        layer["abs_error"] * q2_report["configuration"]["quant_scale"],
                        layer["cumulative_error_bound_grid"] + 1e-6,
                    )

    def test_per_sample_attribution_is_independent_of_batch_boundaries(self) -> None:
        left_config = _config(batch_size=2, sort_strategy="sample_id")
        right_config = _config(batch_size=7, sort_strategy="input_norm")
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            left = InferenceSession(self.assets, snapshot_dir=first_dir).run(left_config)
            right = InferenceSession(self.assets, snapshot_dir=second_dir).run(right_config)

        def collect(report):
            values = {}
            for batch in report["batches"]:
                for sample in batch["samples"]:
                    values[sample["sample_id"]] = {
                        "output": sample["output"],
                        "q_output": sample["q_output"],
                        "diagnostics": sample["diagnostics"],
                    }
            return values

        left_values = collect(left)
        right_values = collect(right)
        self.assertEqual(set(left_values), set(right_values))
        for sample_id in sorted(left_values):
            with self.subTest(sample_id=sample_id):
                self.assertEqual(
                    canonical_json(left_values[sample_id]),
                    canonical_json(right_values[sample_id]),
                )


if __name__ == "__main__":
    unittest.main()
