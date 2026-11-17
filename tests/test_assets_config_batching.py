from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from infer_workbench.assets import generate_assets, load_assets, save_assets
from infer_workbench.batching import plan_batches
from infer_workbench.canonical import canonical_json
from infer_workbench.config import BatchConfig
from infer_workbench.errors import WorkbenchError


ROOT = Path(__file__).resolve().parent.parent


class AssetConfigBatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.assets = load_assets(ROOT / "data")

    def test_generation_is_bitwise_deterministic_and_matches_files(self) -> None:
        one = generate_assets(
            seed=20261117,
            sample_count=12,
            input_dim=4,
            hidden_dims=(6, 5),
            output_dim=3,
        )
        two = generate_assets(
            seed=20261117,
            sample_count=12,
            input_dim=4,
            hidden_dims=(6, 5),
            output_dim=3,
        )
        self.assertEqual(one.data_digest, two.data_digest)
        self.assertEqual(one.parameter_digest, two.parameter_digest)
        self.assertEqual(one.data_digest, self.assets.data_digest)
        self.assertEqual(one.parameter_digest, self.assets.parameter_digest)
        with tempfile.TemporaryDirectory() as temporary:
            save_assets(temporary, one)
            reloaded = load_assets(temporary)
            self.assertEqual(reloaded.data_digest, one.data_digest)
            self.assertEqual(reloaded.parameter_digest, one.parameter_digest)

    def test_tampered_dataset_is_rejected_with_e_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            for name in ("dataset.json", "model.json", "generation.json"):
                shutil.copyfile(ROOT / "data" / name, target / name)
            dataset_path = target / "dataset.json"
            dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
            dataset["samples"][0]["input"][0] = float(dataset["samples"][0]["input"][0]) + 0.25
            dataset_path.write_text(
                json.dumps(dataset, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(WorkbenchError) as raised:
                load_assets(target)
            self.assertEqual(raised.exception.code, "E_DATA")
            self.assertEqual(raised.exception.details["field"], "data_digest")

    def test_config_version_is_canonical_and_scale_must_be_power_of_ten(self) -> None:
        raw = {
            "batch_size": 3,
            "sort_strategy": "input_norm",
            "precision_mode": "fixed",
            "quant_scale": 1000,
            "drift_threshold": 0.005,
        }
        first = BatchConfig.from_dict(raw)
        second = BatchConfig.from_dict(dict(reversed(list(raw.items()))))
        self.assertEqual(first.quant_scale_power, 3)
        self.assertEqual(first.config_version, second.config_version)
        self.assertEqual(
            first.changed_fields(BatchConfig.from_dict({**raw, "quant_scale": 100})),
            ["quant_scale", "quant_scale_power"],
        )

    def test_invalid_configs_return_e_config(self) -> None:
        valid = {
            "batch_size": 3,
            "sort_strategy": "sample_id",
            "precision_mode": "fixed",
            "quant_scale": 1000,
            "drift_threshold": 0.01,
        }
        cases = {
            "zero_batch": {**valid, "batch_size": 0},
            "bool_batch": {**valid, "batch_size": True},
            "bad_sort": {**valid, "sort_strategy": "by_id"},
            "bad_precision": {**valid, "precision_mode": "int8"},
            "bad_scale": {**valid, "quant_scale": 200},
            "bad_power": {**valid, "quant_scale_power": -1, "quant_scale": 1},
            "bad_threshold": {**valid, "drift_threshold": -0.1},
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(WorkbenchError) as raised:
                    BatchConfig.from_dict(raw)
                self.assertEqual(raised.exception.code, "E_CONFIG")

    def test_batch_planning_is_stable_for_all_strategies(self) -> None:
        base = {
            "batch_size": 3,
            "precision_mode": "fixed",
            "quant_scale": 1000,
            "drift_threshold": 0.01,
        }
        plans_by_strategy = {}
        for strategy in ("sample_id", "input_norm", "input_order"):
            config = BatchConfig.from_dict({**base, "sort_strategy": strategy})
            first = plan_batches(self.assets, config)
            second = plan_batches(self.assets, config)
            self.assertEqual(
                [plan.to_dict() for plan in first], [plan.to_dict() for plan in second]
            )
            flattened = [sample_id for plan in first for sample_id in plan.sample_ids]
            self.assertEqual(sorted(flattened), sorted(sample.sample_id for sample in self.assets.samples))
            self.assertEqual([len(plan.sample_ids) for plan in first], [3, 3, 3, 3])
            plans_by_strategy[strategy] = first

        self.assertEqual(
            [sample_id for plan in plans_by_strategy["input_order"] for sample_id in plan.sample_ids],
            [sample.sample_id for sample in self.assets.samples],
        )
        expected_sorted = sorted(sample.sample_id for sample in self.assets.samples)
        self.assertEqual(
            [sample_id for plan in plans_by_strategy["sample_id"] for sample_id in plan.sample_ids],
            expected_sorted,
        )
        self.assertNotEqual(
            canonical_json([plan.to_dict() for plan in plans_by_strategy["input_norm"]]),
            canonical_json([plan.to_dict() for plan in plans_by_strategy["input_order"]]),
        )


if __name__ == "__main__":
    unittest.main()
