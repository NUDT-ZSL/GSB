import unittest

from featbench.config import load_config
from featbench.data import generate_dataset
from featbench.errors import BenchError
from tests._util import TempWorkdir, write_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempWorkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_fingerprint_is_stable_and_derived(self):
        path = write_config(self.tmp.path, {})
        config_a = load_config(path)
        config_b = load_config(path)
        self.assertEqual(config_a.config_fingerprint, config_b.config_fingerprint)
        self.assertEqual(len(config_a.config_fingerprint), 32)

    def test_unknown_column_type_rejected(self):
        path = write_config(self.tmp.path, {"columns": [
            {"name": "bad", "type": "mystery"},
            {"name": "target", "type": "numeric"},
        ], "target_column": "target"})
        with self.assertRaises(BenchError) as ctx:
            load_config(path)
        self.assertEqual(ctx.exception.code, "E_CONFIG_INVALID")

    def test_missing_target_rejected(self):
        path = write_config(self.tmp.path, {"target_column": "ghost"})
        with self.assertRaises(BenchError) as ctx:
            load_config(path)
        self.assertEqual(ctx.exception.code, "E_TARGET_MISSING")

    def test_illegal_seed_and_scale_rejected(self):
        path = write_config(self.tmp.path, {"seed": -1})
        with self.assertRaises(BenchError) as ctx:
            load_config(path)
        self.assertEqual(ctx.exception.code, "E_CONFIG_INVALID")
        path = write_config(self.tmp.path, {"scales": [
            {"id": "x", "samples": 0}]})
        with self.assertRaises(BenchError) as ctx:
            load_config(path)
        self.assertEqual(ctx.exception.code, "E_CONFIG_INVALID")

    def test_unknown_whitelist_column_rejected(self):
        path = write_config(self.tmp.path, {"cross": {
            "min_order": 1, "max_order": 1, "whitelist": ["nope"],
            "cardinality_budget": 4, "degrade_strategy": "skip"}})
        with self.assertRaises(BenchError) as ctx:
            load_config(path)
        self.assertEqual(ctx.exception.code, "E_CONFIG_INVALID")


class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempWorkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_generation_is_deterministic_and_scaled(self):
        path = write_config(self.tmp.path, {})
        config = load_config(path)
        scale = config.scales[0]
        first = generate_dataset(config, scale)
        second = generate_dataset(config, scale)
        self.assertEqual(first.generation_fingerprint, second.generation_fingerprint)
        import numpy as np
        np.testing.assert_array_equal(first.target, second.target)
        scaled_path = write_config(self.tmp.path, {"scales": [
            {"id": "s1", "samples": 160, "feature_multiplier": 2.0,
             "cardinality_multiplier": 3.0}]})
        scaled_config = load_config(scaled_path)
        scaled = generate_dataset(scaled_config, scaled_config.scales[0])
        self.assertEqual(scaled.samples, 160)
        self.assertGreater(len(scaled.columns), len(first.columns))
        self.assertEqual(scaled.scale_cardinality["cat_high"], 90)

    def test_missing_and_redundant_columns_present(self):
        path = write_config(self.tmp.path, {})
        config = load_config(path)
        dataset = generate_dataset(config, config.scales[0])
        import numpy as np
        self.assertTrue(np.isnan(dataset.numeric).any())
        self.assertIn("num_red", dataset.numeric_names)


if __name__ == "__main__":
    unittest.main()
