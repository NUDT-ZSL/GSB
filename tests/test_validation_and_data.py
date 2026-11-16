from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from regularization_lab.config import load_experiment
from regularization_lab.data import build_dataset, load_data_spec
from regularization_lab.lab import ExperimentSession

from tests.helpers import ROOT, write_yaml


class ValidationAndDataTests(unittest.TestCase):
    def test_sample_data_is_deterministic_and_fully_imputed(self) -> None:
        config = load_experiment(ROOT / "configs" / "experiment.yaml")
        first, first_failures = build_dataset(config)
        second, second_failures = build_dataset(config)
        self.assertEqual(first_failures, [])
        self.assertEqual(second_failures, [])
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.data_fingerprint, second.data_fingerprint)
        np.testing.assert_array_equal(first.X_train, second.X_train)
        np.testing.assert_array_equal(first.y_train, second.y_train)
        self.assertTrue(np.all(np.isfinite(first.X_train)))
        self.assertTrue(np.all(np.isfinite(first.X_validation)))
        self.assertIn("region=__MISSING__", first.feature_names)
        self.assertEqual(first.train_size, 256)
        self.assertEqual(first.validation_size, 64)

    def test_invalid_fields_are_isolated_and_valid_scheme_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_path = write_yaml(
                root / "data.yaml",
                {
                    "schema_version": 1,
                    "seed": 7,
                    "rows": 40,
                    "target_column": "target",
                    "columns": [
                        {"name": "x", "type": "numeric", "distribution": "normal", "mean": 0, "std": 1},
                        {
                            "name": "c",
                            "type": "categorical",
                            "categories": ["a", "b"],
                            "missing_rate": 0.1,
                        },
                        {"name": "bad_copy", "type": "redundant", "source": "ghost"},
                        {
                            "name": "target",
                            "type": "target",
                            "formula": {
                                "bias": 1.0,
                                "coefficients": {"x": 1.5, "ghost": 99.0},
                            },
                            "noise_std": 0.0,
                        },
                    ],
                },
            )
            config_path = write_yaml(
                root / "experiment.yaml",
                {
                    "schema_version": 1,
                    "data": {
                        "spec_path": str(data_path),
                        "feature_columns": ["x", "c", "unknown_feature"],
                        "expected_column_types": {"c": "numeric", "ghost": "numeric"},
                    },
                    "optimizer": {
                        "learning_rate": 0.2,
                        "max_iterations": 5,
                        "tolerance": 100.0,
                        "batch_size": 8,
                    },
                    "split": {"train_ratio": 0.75, "seed": 3},
                    "regularizers": [
                        {"name": "bad_ridge", "type": "l2", "alpha": -0.1},
                        {"name": "good", "type": "l2", "alpha": 0.01, "l1_ratio": 0.0},
                    ],
                    "runtime": {
                        "concurrency": 0,
                        "snapshot_commit_interval": 0,
                        "numeric_tolerance": 1e-12,
                        "state_dir": str(root / "state"),
                        "report_path": str(root / "report.json"),
                    },
                },
            )
            session = ExperimentSession.from_config_path(config_path)
            self.assertIsNotNone(session.dataset)
            codes = {item["business_code"] for item in session.failures}
            self.assertIn("E_CONFIG_INVALID", codes)
            messages = " ".join(item["message"] for item in session.failures)
            self.assertIn("alpha must not be negative", messages)
            self.assertIn("concurrency", messages)
            self.assertIn("unknown_feature", messages)
            self.assertIn("type mismatch", messages)
            self.assertIn("unknown source", messages)
            self.assertIn("unknown column", messages)
            self.assertEqual(session.config.runtime.concurrency, 1)
            self.assertNotIn("bad_ridge", session.schemes)
            result = session.run_scheme("good")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["status"], "complete")
            report = session.write_report()
            self.assertIn("E_CONFIG_INVALID", report["business_codes"])
            self.assertEqual(report["schemes"]["bad_ridge"]["status"], "invalid")

    def test_missing_target_is_reported_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_path = write_yaml(
                root / "data.yaml",
                {
                    "schema_version": 1,
                    "seed": 1,
                    "rows": 10,
                    "target_column": "target",
                    "columns": [
                        {"name": "x", "type": "numeric", "distribution": "normal"},
                        {"name": "c", "type": "categorical", "categories": ["a", "b"]},
                    ],
                },
            )
            config_path = write_yaml(
                root / "experiment.yaml",
                {
                    "schema_version": 1,
                    "data": {"spec_path": str(data_path)},
                    "optimizer": {"max_iterations": 2},
                    "split": {"train_ratio": 0.8, "seed": 1},
                    "regularizers": [{"name": "ridge", "type": "l2", "alpha": 0.1}],
                    "runtime": {
                        "concurrency": 1,
                        "state_dir": str(root / "state"),
                        "report_path": str(root / "report.json"),
                    },
                },
            )
            session = ExperimentSession.from_config_path(config_path)
            self.assertIsNone(session.dataset)
            report = session.write_report()
            self.assertEqual(report["status"], "failed")
            self.assertIn("E_CONFIG_INVALID", report["business_codes"])
            self.assertTrue(any(item["column"] == "target" for item in report["failures"]))


if __name__ == "__main__":
    unittest.main()
