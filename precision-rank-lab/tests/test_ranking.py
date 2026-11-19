import unittest

from prank.config import build_config
from prank.errors import BusinessError
from prank.models import Item
from prank.precision import make_context
from prank.ranking import (diversity_rerank, gate_decisions,
                           verify_monotonicity)


def _item(item_id, category):
    return Item(item_id, ("t001",), category, 10.0, 0.5)


class RerankTests(unittest.TestCase):
    def setUp(self):
        cfg_dict = {
            "feature_weights": {"term_match": 1.0}, "threshold": 0.5,
            "tolerance": 0.01, "gate_strategy": "tolerance_band",
            "top_k": 5, "rerank_n": 5,
            "rerank": {"category_penalty": 0.2, "position_decay": 0.9},
            "precision": {"fixed": {"fraction_bits": 16, "scale": 65536,
                                    "rounding": "half_even", "int_bits": 62,
                                    "exp_terms": 24}},
            "bounds": {"price_min": 0.01, "price_max": 100000.0,
                       "quality_min": 0.0, "quality_max": 1.0},
            "degradation": {"policy": "abort", "allowed_policies": ["abort"]},
            "report": {"interval_quantiles": [0.0, 0.5, 1.0],
                       "near_threshold_delta": 0.02},
        }
        self.cfg = build_config(cfg_dict)

    def test_duplicate_input_raises_inconsistent(self):
        ctx = make_context("float64", self.cfg.fixed)
        items = {"a": _item("a", "x"), "b": _item("b", "y")}
        with self.assertRaises(BusinessError) as exc:
            diversity_rerank(["a", "a"], items,
                             {"a": ctx.encode(0.9)}, ctx,
                             0.2, 0.9, "q1")
        self.assertEqual(exc.exception.code, "E_INCONSISTENT")
        self.assertEqual(exc.exception.query_id, "q1")

    def test_penalty_and_decay_trace(self):
        ctx = make_context("float64", self.cfg.fixed)
        items = {"a": _item("a", "x"), "b": _item("b", "x"),
                 "c": _item("c", "y")}
        scores = {iid: ctx.encode(score) for iid, score in
                  (("a", 0.95), ("b", 0.94), ("c", 0.90))}
        order, trace = diversity_rerank(["a", "b", "c"], items, scores, ctx,
                                        0.2, 0.9, "q1")
        self.assertEqual(sorted(order), ["a", "b", "c"])
        self.assertTrue(verify_monotonicity(trace))
        # second item of the same category must carry the penalty flag
        same_cat = [row for row in trace if row["category"] == "x"]
        self.assertTrue(same_cat[1]["category_penalty_applied"])
        self.assertFalse(same_cat[0]["category_penalty_applied"])

    def test_tolerance_band_keeps_near_threshold_stable(self):
        ctx = make_context("float32", self.cfg.fixed)
        scores = ctx.encode([0.415, 0.5, 0.425])
        rows = gate_decisions(scores, ["i1", "i2", "i3"], ctx,
                              0.42, 0.01, "tolerance_band", 0.05)
        # 0.415 sits below the strict threshold but inside the band
        self.assertFalse(rows[0]["strict_pass"])
        self.assertTrue(rows[0]["band_pass"])
        self.assertTrue(rows[0]["admitted"])


if __name__ == "__main__":
    unittest.main()
