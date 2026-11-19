import math
import unittest

from prank.config import build_config
from prank.precision import _round_divide, make_context


DEFAULT = {
    "feature_weights": {"a": 1.0}, "threshold": 0.5, "tolerance": 0.01,
    "gate_strategy": "tolerance_band", "top_k": 3, "rerank_n": 3,
    "rerank": {"category_penalty": 0.2, "position_decay": 0.9},
    "precision": {"tiers": ["float64", "float32", "fixed"],
                  "fixed": {"fraction_bits": 16, "scale": 65536,
                            "rounding": "half_even", "int_bits": 62,
                            "exp_terms": 24}},
    "bounds": {"price_min": 0.01, "price_max": 100000.0,
               "quality_min": 0.0, "quality_max": 1.0},
    "degradation": {"policy": "fallback_float64",
                    "allowed_policies": ["abort", "skip_query", "fallback_float64"]},
    "report": {"interval_quantiles": [0.0, 0.5, 0.95, 1.0],
               "near_threshold_delta": 0.02},
}


class RoundingTests(unittest.TestCase):
    def test_rounding_modes(self):
        # 2.5 scaled representations exercise the tie rules
        self.assertEqual(_round_divide(5, 2, "trunc"), 2)
        self.assertEqual(_round_divide(-5, 2, "trunc"), -2)
        self.assertEqual(_round_divide(-5, 2, "floor"), -3)
        self.assertEqual(_round_divide(5, 2, "half_up"), 3)
        self.assertEqual(_round_divide(-5, 2, "half_up"), -3)
        self.assertEqual(_round_divide(5, 2, "half_even"), 2)
        self.assertEqual(_round_divide(7, 2, "half_even"), 4)

    def test_unknown_precision(self):
        from prank.errors import BusinessError
        cfg = build_config(DEFAULT)
        with self.assertRaises(BusinessError) as ctx:
            make_context("float16", cfg.fixed)
        self.assertEqual(ctx.exception.code, "E_PRECISION_UNKNOWN")


class FixedSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = build_config(DEFAULT)
        self.ctx = make_context("fixed", self.cfg.fixed)

    def test_sigmoid_matches_reference(self):
        xs = [-5.0, -1.0, -0.25, 0.0, 0.25, 1.0, 5.0]
        got = self.ctx.sigmoid(self.ctx.encode(xs))
        for x, g in zip(xs, got):
            expected = 1.0 / (1.0 + math.exp(-x))
            self.assertAlmostEqual(self.ctx.decode(g), expected, delta=2e-5)

    def test_exp_matches_reference(self):
        xs = [-3.0, -0.5, 0.0, 0.5, 3.0]
        got = self.ctx.exp(self.ctx.encode(xs))
        for x, g in zip(xs, got):
            self.assertAlmostEqual(self.ctx.decode(g), math.exp(x), delta=2e-4)

    def test_linear_dot_shared_semantics(self):
        import numpy as np
        matrix = np.array([[0.8, 0.0, 0.55, 0.9],
                           [0.2, 1.0, 0.75, 0.3]])
        weights = [2.4, 1.1, 0.8, 1.5]
        for mode in ("float64", "float32", "fixed"):
            ctx = make_context(mode, self.cfg.fixed)
            encoded = ctx.encode(matrix)
            out = ctx.linear_dot(encoded, weights, -1.2)
            ref = matrix @ np.array(weights) - 1.2
            got = ctx.decode(out) if mode == "fixed" else np.asarray(out, np.float64)
            np.testing.assert_allclose(got, ref, rtol=1e-4, atol=1e-4)

    def test_no_overflow_on_large_products(self):
        big = self.ctx.encode([1000.0, -1000.0])
        product = self.ctx.mul(big, big)
        self.assertEqual(self.ctx.decode(product[0]), 1_000_000.0)


if __name__ == "__main__":
    unittest.main()
