import json
import tempfile
import unittest
from pathlib import Path

from prank.config import load_config
from prank.errors import BusinessError
from prank.snapshot import (checksum_of, load_snapshot, next_version,
                            submit_snapshot)


BASE_CONFIG = {
    "config_version": 1,
    "feature_weights": {"term_match": 2.4, "category_match": 1.1,
                        "price_bucket": 0.8, "quality_scaled": 1.5},
    "score_bias": -1.2, "threshold": 0.42, "tolerance": 0.01,
    "gate_strategy": "tolerance_band", "top_k": 10, "rerank_n": 10,
    "rerank": {"category_penalty": 0.18, "position_decay": 0.85},
    "precision": {"tiers": ["float64", "float32", "fixed"],
                  "fixed": {"fraction_bits": 16, "scale": 65536,
                            "rounding": "half_even", "int_bits": 62,
                            "exp_terms": 24}},
    "bounds": {"price_min": 0.01, "price_max": 100000.0,
               "quality_min": 0.0, "quality_max": 1.0},
    "degradation": {"policy": "abort",
                    "allowed_policies": ["abort", "skip_query",
                                         "fallback_float64"]},
    "report": {"interval_quantiles": [0.0, 0.5, 0.95, 1.0],
               "near_threshold_delta": 0.02},
}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "snap.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_version_monotonic_and_wholesale_replace(self):
        submit_snapshot(self.path, "config", {"v": 1}, next_version(self.path))
        submit_snapshot(self.path, "config", {"v": 2}, next_version(self.path))
        envelope = load_snapshot(self.path, expected_category="config")
        self.assertEqual(envelope["version"], 2)
        self.assertEqual(envelope["payload"], {"v": 2})
        self.assertEqual(envelope["checksum"], checksum_of({"v": 2}))

    def test_corrupt_checksum_raises_with_category_and_version(self):
        submit_snapshot(self.path, "statistics", {"x": 1}, 3)
        envelope = json.loads(self.path.read_text(encoding="utf-8"))
        envelope["payload"]["x"] = 999
        self.path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaises(BusinessError) as ctx:
            load_snapshot(self.path, expected_category="statistics")
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(ctx.exception.context["snapshot_category"], "statistics")
        self.assertEqual(ctx.exception.context["version"], 3)

    def test_category_mismatch(self):
        submit_snapshot(self.path, "config", {"x": 1}, 1)
        with self.assertRaises(BusinessError) as ctx:
            load_snapshot(self.path, expected_category="statistics")
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_CORRUPT")


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.path.write_text(json.dumps(BASE_CONFIG), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _expect(self, overrides, code, field=None):
        with self.assertRaises(BusinessError) as ctx:
            load_config(self.path, overrides)
        self.assertEqual(ctx.exception.code, code)
        if field:
            self.assertEqual(ctx.exception.field, field)

    def test_invalid_threshold_and_tolerance(self):
        self._expect({"threshold": 1.5}, "E_CONFIG_INVALID", "threshold")
        self._expect({"tolerance": -0.2}, "E_CONFIG_INVALID", "tolerance")

    def test_unknown_precision_tier(self):
        self._expect({"precision.tiers": ["float64", "bfloat16"]},
                     "E_PRECISION_UNKNOWN")

    def test_non_positive_scale(self):
        self._expect({"precision.fixed.scale": 0}, "E_SCALE_INVALID",
                     "precision.fixed.scale")
        self._expect({"precision.fixed.scale": -4}, "E_SCALE_INVALID")

    def test_override_merges(self):
        cfg = load_config(self.path, {"threshold": 0.6, "top_k": 5})
        self.assertEqual(cfg.threshold, 0.6)
        self.assertEqual(cfg.top_k, 5)


if __name__ == "__main__":
    unittest.main()
