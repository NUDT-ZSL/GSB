import json
import os
import tempfile
import unittest

from ranker.config import load_config, load_groups
from ranker.errors import ConfigInvalid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigValidationTest(unittest.TestCase):
    def setUp(self):
        self.base = json.load(open(os.path.join(ROOT, "params", "rank_config.json")))
        self.tmp = tempfile.mkdtemp()

    def load(self, mutated):
        path = os.path.join(self.tmp, "cfg.json")
        json.dump(mutated, open(path, "w"), indent=2)
        return load_config(path)

    def test_weight_dim_mismatch(self):
        bad = dict(self.base)
        bad["weights"] = self.base["weights"][:-1]
        with self.assertRaises(ConfigInvalid) as ctx:
            self.load(bad)
        self.assertEqual(ctx.exception.code, "E_CONFIG_INVALID")

    def test_frac_bits_out_of_range(self):
        for value in (-1, 32):
            bad = dict(self.base)
            bad["frac_bits"] = value
            with self.assertRaises(ConfigInvalid):
                self.load(bad)

    def test_window_must_be_positive(self):
        bad = dict(self.base)
        bad["rerank_window"] = 0
        with self.assertRaises(ConfigInvalid):
            self.load(bad)

    def test_bad_rounding_and_clip(self):
        bad = dict(self.base)
        bad["rounding"] = "weird"
        with self.assertRaises(ConfigInvalid):
            self.load(bad)
        bad2 = dict(self.base)
        bad2["score_clip"] = [1.0, -1.0]
        with self.assertRaises(ConfigInvalid):
            self.load(bad2)

    def test_runtime_keys_excluded_from_hash(self):
        a = dict(self.base)
        b = dict(self.base)
        b["max_workers"] = 8
        b["snapshot_interval"] = 5
        self.assertEqual(self.load(a)["scientific_hash"],
                         self.load(b)["scientific_hash"])

    def test_scientific_change_moves_hash(self):
        a = dict(self.base)
        b = dict(self.base)
        b["frac_bits"] = 5
        self.assertNotEqual(self.load(a)["scientific_hash"],
                            self.load(b)["scientific_hash"])

    def test_group_table_loads_and_hashes(self):
        table = load_groups(os.path.join(ROOT, "params", "feature_groups.json"))
        self.assertTrue(table["groups"])
        self.assertEqual(len(table["hash"]), 16)


if __name__ == "__main__":
    unittest.main()
