import unittest

from prank.config import build_config
from prank.precision import make_context
from prank.ranking import gate_decisions


CONFIG = {
    "feature_weights": {"term_match": 1.0}, "threshold": 0.5,
    "tolerance": 0.01, "gate_strategy": "tolerance_band", "top_k": 3,
    "rerank_n": 3, "rerank": {"category_penalty": 0.1, "position_decay": 0.9},
    "precision": {"fixed": {"fraction_bits": 16, "scale": 65536,
                            "rounding": "half_even", "int_bits": 62,
                            "exp_terms": 24}},
    "bounds": {"price_min": 0.01, "price_max": 100000.0,
               "quality_min": 0.0, "quality_max": 1.0},
    "degradation": {"policy": "abort", "allowed_policies": ["abort"]},
    "report": {"interval_quantiles": [0.0, 0.5, 1.0],
               "near_threshold_delta": 0.02},
}


class GateStabilityTests(unittest.TestCase):
    def setUp(self):
        self.cfg = build_config(CONFIG)

    def test_near_threshold_decisions_are_stable_across_tiers_and_reruns(self):
        # values straddling the 0.5 threshold within and outside the band
        values = [0.4999, 0.495, 0.5, 0.5001, 0.51]
        item_ids = [f"i{k}" for k in range(len(values))]
        decisions = {}
        for mode in ("float64", "float32", "fixed"):
            ctx = make_context(mode, self.cfg.fixed)
            rows = gate_decisions(ctx.encode(values), item_ids, ctx,
                                  0.5, 0.01, "tolerance_band", 0.02)
            decisions[mode] = [row["admitted"] for row in rows]
            # repeated evaluation must be identical (no run-to-run flips)
            rows_again = gate_decisions(ctx.encode(values), item_ids, ctx,
                                        0.5, 0.01, "tolerance_band", 0.02)
            self.assertEqual([r["admitted"] for r in rows_again],
                             decisions[mode])
        # every tier admits the same candidates under the band strategy
        self.assertEqual(decisions["float64"], decisions["float32"])
        self.assertEqual(decisions["float64"], decisions["fixed"])

    def test_strict_strategy_may_differ_but_is_deterministic(self):
        values = [0.4999999]
        ctx32 = make_context("float32", self.cfg.fixed)
        rows = gate_decisions(ctx32.encode(values), ["x"], ctx32,
                              0.5, 0.01, "strict", 0.02)
        rows2 = gate_decisions(ctx32.encode(values), ["x"], ctx32,
                               0.5, 0.01, "strict", 0.02)
        self.assertEqual(rows[0]["strict_pass"], rows2[0]["strict_pass"])


if __name__ == "__main__":
    unittest.main()
