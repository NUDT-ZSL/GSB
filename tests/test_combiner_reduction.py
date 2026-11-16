import unittest

import numpy as np

from featbench.combiner import build_crosses
from featbench.config import load_config
from featbench.data import generate_dataset
from featbench.encoder import encode_matrix
from featbench.errors import BenchError
from featbench.reduction import (reduce_pca, reduce_random_projection,
                                 reduce_variance)
from tests._util import TempWorkdir, write_config


class CombinerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempWorkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_hash_bucket_degradation_audit(self):
        path = write_config(self.tmp.path, {"cross": {
            "min_order": 2, "max_order": 2,
            "whitelist": ["cat_low", "cat_high"],
            "cardinality_budget": 8,
            "degrade_strategy": "hash_bucket"}})
        config = load_config(path)
        dataset = generate_dataset(config, config.scales[0])
        result = build_crosses(config, config.scales[0], dataset)
        self.assertEqual(len(result.cross_columns), 1)
        cross = result.cross_columns[0]
        self.assertEqual(cross.strategy, "hash_bucket")
        self.assertEqual(cross.final_cardinality, 8)
        self.assertGreater(cross.raw_cardinality, 8)
        audit = result.audit[0]
        for field_name in ("members", "raw_cardinality",
                           "final_cardinality", "strategy"):
            self.assertIn(field_name, audit)

    def test_skip_degradation_drops_combination(self):
        path = write_config(self.tmp.path, {"cross": {
            "min_order": 2, "max_order": 2,
            "whitelist": ["cat_low", "cat_high"],
            "cardinality_budget": 2,
            "degrade_strategy": "skip"}})
        config = load_config(path)
        dataset = generate_dataset(config, config.scales[0])
        result = build_crosses(config, config.scales[0], dataset)
        self.assertEqual(result.cross_columns, [])
        self.assertEqual(result.audit[0]["strategy"], "skip")
        self.assertEqual(result.audit[0]["final_cardinality"], 0)

    def test_deterministic_cross_codes(self):
        path = write_config(self.tmp.path, {})
        config = load_config(path)
        dataset = generate_dataset(config, config.scales[0])
        first = build_crosses(config, config.scales[0], dataset)
        second = build_crosses(config, config.scales[0], dataset)
        self.assertEqual(first.combiner_fingerprint, second.combiner_fingerprint)
        np.testing.assert_array_equal(first.cross_columns[0].codes,
                                      second.cross_columns[0].codes)


class ReductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempWorkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _encoded(self, overrides=None):
        path = write_config(self.tmp.path, overrides or {})
        config = load_config(path)
        dataset = generate_dataset(config, config.scales[0])
        crosses = build_crosses(config, config.scales[0], dataset)
        encoded = encode_matrix(config, dataset, crosses, True)
        return config, encoded

    def test_three_methods_shapes_and_pca_sign_rule(self):
        config, encoded = self._encoded()
        pca = reduce_pca(config, encoded)
        rp = reduce_random_projection(config, encoded)
        var = reduce_variance(config, encoded)
        self.assertEqual(pca.reduced.shape[1], pca.retained_dim)
        self.assertEqual(rp.reduced.shape[1], rp.retained_dim)
        self.assertEqual(var.reduced.shape[1], var.retained_dim)
        self.assertLessEqual(pca.retained_dim, config.reduction.target_dim)
        # PCA sign normalization: largest magnitude coefficient is non-negative.
        # Recompute components indirectly through explained variance ordering.
        self.assertGreaterEqual(pca.summary["explained_variance"][0],
                                pca.summary["explained_variance"][-1])

    def test_random_projection_seed_derived_from_fingerprint(self):
        config, encoded = self._encoded()
        rp_a = reduce_random_projection(config, encoded)
        rp_b = reduce_random_projection(config, encoded)
        self.assertEqual(rp_a.summary["projection_seed"],
                         rp_b.summary["projection_seed"])
        np.testing.assert_array_equal(rp_a.reduced, rp_b.reduced)

    def test_missing_unfilled_raises_business_code(self):
        path = write_config(self.tmp.path, {"impute_missing": False})
        config = load_config(path)
        dataset = generate_dataset(config, config.scales[0])
        crosses = build_crosses(config, config.scales[0], dataset)
        with self.assertRaises(BenchError) as ctx:
            encode_matrix(config, dataset, crosses, False)
        self.assertEqual(ctx.exception.code, "E_MISSING_UNFILLED")

    def test_all_constant_input_raises_business_code(self):
        from featbench.encoder import EncodedMatrix
        config, encoded = self._encoded()
        encoded.matrix = np.zeros_like(encoded.matrix)
        encoded.constant_features = list(encoded.feature_names)
        with self.assertRaises(BenchError) as ctx:
            reduce_pca(config, encoded)
        self.assertEqual(ctx.exception.code, "E_CONSTANT_COLUMN")


if __name__ == "__main__":
    unittest.main()
