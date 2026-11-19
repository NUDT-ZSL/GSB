from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from numeric_workbench.config import TrainingConfig
from numeric_workbench.data import generate_deterministic_samples, load_dataset
from numeric_workbench.defaults import default_config_dict
from numeric_workbench.errors import WorkbenchError
from numeric_workbench.registry import JobRegistry


class ConfigValidationTests(unittest.TestCase):
    def assert_config_field(self, field: str, **overrides):
        payload = default_config_dict("config-field-test")
        payload.update(overrides)
        with self.assertRaises(WorkbenchError) as context:
            TrainingConfig.from_dict(payload)
        self.assertEqual(context.exception.code, "E_CONFIG_INVALID")
        self.assertEqual(context.exception.details["field"], field)

    def test_unknown_precision(self):
        self.assert_config_field("precision", precision="float16")

    def test_scale_must_be_positive_and_bounded(self):
        self.assert_config_field("fixed_scale", fixed_scale=0)
        self.assert_config_field("fixed_scale", fixed_scale=TrainingConfig.MAX_FIXED_SCALE + 1)

    def test_regularization_bounds(self):
        self.assert_config_field("regularization_strength", regularization_strength=-0.1)
        self.assert_config_field(
            "regularization_strength",
            regularization_strength=TrainingConfig.MAX_REGULARIZATION + 0.1,
        )

    def test_chunk_size_must_be_positive(self):
        self.assert_config_field("chunk_size", chunk_size=0)

    def test_convergence_tolerance_must_be_positive(self):
        self.assert_config_field("convergence_tolerance", convergence_tolerance=0)

    def test_rounding_and_strategy_are_enumerated(self):
        self.assert_config_field("rounding_mode", rounding_mode="magic")
        self.assert_config_field("convergence_strategy", convergence_strategy="magic")

    def test_columns_must_be_disjoint(self):
        self.assert_config_field(
            "columns",
            numeric_columns=["num_0"],
            categorical_columns=["num_0"],
        )


class DataValidationTests(unittest.TestCase):
    def test_same_seed_produces_byte_identical_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "one.jsonl"
            second = Path(directory) / "two.jsonl"
            manifest_one = generate_deterministic_samples(first, row_count=100, seed=77)
            manifest_two = generate_deterministic_samples(second, row_count=100, seed=77)
            self.assertEqual(manifest_one["sha256"], manifest_two["sha256"])
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_duplicate_row_id_has_business_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.jsonl"
            config = TrainingConfig.from_dict(default_config_dict("duplicate-row"))
            record = {
                "row_id": "r1",
                "num_0": 0.1,
                "num_1": 0.2,
                "num_2": 0.3,
                "cat_0": "alpha",
                "cat_1": "north",
                "label": "yes",
            }
            second = dict(record)
            second["label"] = "no"
            path.write_text(
                json.dumps(record) + "\n" + json.dumps(second) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(WorkbenchError) as context:
                load_dataset(path, config)
            self.assertEqual(context.exception.code, "E_DUPLICATE_ROW_ID")
            self.assertEqual(context.exception.details["row_id"], "r1")

    def test_duplicate_job_id_has_business_code(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = JobRegistry(directory)
            registry.register("job-a", input_sha256="i", config_sha256="c")
            with self.assertRaises(WorkbenchError) as context:
                registry.register("job-a", input_sha256="i", config_sha256="c")
            self.assertEqual(context.exception.code, "E_DUPLICATE_JOB_ID")
            self.assertEqual(context.exception.details["job_id"], "job-a")


if __name__ == "__main__":
    unittest.main()
