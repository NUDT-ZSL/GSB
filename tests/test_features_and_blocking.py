from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from diagnostic_engine.blocking import decide_block_plan, whole_block_plan
from diagnostic_engine.data import load_jsonl_dataset, prepare_features
from diagnostic_engine.generator import generate_dataset
from diagnostic_engine.injection import InjectionController
from diagnostic_engine.snapshots import parse_v1
from diagnostic_engine.utils import canonical_json_bytes


class FeatureAndBlockingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.generated = generate_dataset(cls.root / "data", sample_count=96)
        cls.config = parse_v1(cls.generated.v1_path)
        cls.dataset = load_jsonl_dataset(cls.generated.samples_path, cls.config)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_standardization_and_encoding_are_deterministic(self) -> None:
        first = prepare_features(self.dataset, self.config)
        second = prepare_features(self.dataset, self.config)
        np.testing.assert_array_equal(first.matrix, second.matrix)
        self.assertEqual(first.feature_names, second.feature_names)
        numeric_count = len(self.config.numeric_columns)
        np.testing.assert_allclose(
            np.mean(first.matrix[:, :numeric_count], axis=0),
            np.zeros(numeric_count),
            atol=1e-12,
        )
        np.testing.assert_allclose(
            np.std(first.matrix[:, :numeric_count], axis=0, ddof=0),
            np.ones(numeric_count),
            atol=1e-12,
        )
        category_width = first.matrix.shape[1] - numeric_count
        category_rows = first.matrix[:, numeric_count:]
        self.assertEqual(category_width, 7)
        self.assertTrue(
            np.all(np.sum(category_rows, axis=1) == len(self.config.category_columns))
        )

    def test_unknown_category_degrades_to_zero_vector(self) -> None:
        controller = InjectionController.from_specs(
            [
                {
                    "stage": "feature_standardization",
                    "code": "CATEGORY_OUT_OF_RANGE",
                    "row_index": 0,
                    "column": "region",
                    "value": "__UNKNOWN__",
                }
            ]
        )
        prepared = prepare_features(self.dataset, self.config, controller)
        self.assertTrue(prepared.issues)
        self.assertEqual(
            prepared.issues[0].degrade_category, "unknown_category_zero_fill"
        )
        numeric_count = len(self.config.numeric_columns)
        region_width = len(self.config.category_vocab["region"])
        np.testing.assert_array_equal(
            prepared.matrix[0, numeric_count : numeric_count + region_width],
            np.zeros(region_width),
        )

    def test_block_decision_is_byte_stable_and_full_coverage(self) -> None:
        first = decide_block_plan(
            sample_count=self.dataset.sample_count,
            feature_count=20,
            config=self.config,
        )
        second = decide_block_plan(
            sample_count=self.dataset.sample_count,
            feature_count=20,
            config=self.config,
        )
        self.assertEqual(
            canonical_json_bytes(first.to_dict()),
            canonical_json_bytes(second.to_dict()),
        )
        self.assertEqual(first.blocks[0][0], 0)
        self.assertEqual(first.blocks[-1][1], self.dataset.sample_count)
        for (_, previous_stop), (next_start, _) in zip(
            first.blocks, first.blocks[1:]
        ):
            self.assertEqual(previous_stop, next_start)

    def test_budget_triggers_small_block_degrade(self) -> None:
        config = self.config.with_updates(block_size=256, block_budget_bytes=2048)
        plan = decide_block_plan(
            sample_count=self.dataset.sample_count,
            feature_count=20,
            config=config,
        )
        self.assertTrue(plan.small_block_degrade)
        self.assertLess(plan.block_size, 256)
        whole = whole_block_plan(
            sample_count=self.dataset.sample_count,
            feature_count=20,
            config=config,
        )
        self.assertEqual(len(whole.blocks), 1)
        self.assertEqual(whole.blocks[0], (0, self.dataset.sample_count))

    def test_zero_block_size_parameter_degrades(self) -> None:
        controller = InjectionController()
        config = self.config.with_updates(block_size=0)
        plan = decide_block_plan(
            sample_count=self.dataset.sample_count,
            feature_count=20,
            config=config,
            injector=controller,
        )
        self.assertGreater(plan.block_size, 0)
        self.assertTrue(plan.small_block_degrade)
        self.assertEqual(
            controller.issues[-1].degrade_category, "auto_block_plan"
        )


if __name__ == "__main__":
    unittest.main()

