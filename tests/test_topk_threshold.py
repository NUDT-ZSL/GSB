import unittest

from xrel.config import STRICT, TOLERANCE_BAND, ThresholdConfig
from xrel.errors import XrelError
from xrel.numerics import Float32Backend, Float64Backend
from xrel.scoring import DocScore
from xrel.topk import select_topk


def scores():
    return {
        "d1": DocScore("d1", "en", 6.0000001),
        "d2": DocScore("d2", "en", 5.9999999),
        "d3": DocScore("d3", "en", 3.0),
        "d4": DocScore("d4", "en", 1.0),
    }


class ThresholdTests(unittest.TestCase):
    def test_strict_strategy_keeps_boundary_decisions_exact(self):
        config = ThresholdConfig(strategy=STRICT, tolerance=0.0)
        result = select_topk("q", scores(), 4, config, Float64Backend())
        grades = {item.doc_id: item.grade for item in result.selected}
        self.assertEqual(grades["d1"], "relevant")
        self.assertEqual(grades["d2"], "weak")
        self.assertEqual(result.protected, [])

    def test_tolerance_band_protects_boundary_document(self):
        config = ThresholdConfig(strategy=TOLERANCE_BAND, tolerance=1e-6)
        result = select_topk("q", scores(), 4, config, Float64Backend())
        grades = {item.doc_id: item.grade for item in result.selected}
        # d2 sits within tolerance below the 6.0 boundary and is upgraded.
        self.assertEqual(grades["d2"], "relevant")
        protected_docs = {item.get("doc_id") for item in result.protected}
        self.assertIn("d2", protected_docs)

    def test_invalid_strategy_and_tolerance_raise_business_codes(self):
        with self.assertRaises(XrelError) as strategy_ctx:
            select_topk(
                "q", scores(), 4,
                ThresholdConfig(strategy="nonsense"), Float64Backend(),
            )
        self.assertEqual(strategy_ctx.exception.code, "E_THRESHOLD_STRATEGY_INVALID")
        with self.assertRaises(XrelError) as tolerance_ctx:
            select_topk(
                "q", scores(), 4,
                ThresholdConfig(strategy=TOLERANCE_BAND, tolerance=-1.0),
                Float64Backend(),
            )
        self.assertEqual(tolerance_ctx.exception.code, "E_THRESHOLD_TOLERANCE_INVALID")

    def test_float32_subnormal_is_flushed_and_recorded(self):
        backend = Float32Backend()
        data = {"tiny": DocScore("tiny", "en", 1e-40), "ok": DocScore("ok", "en", 2.0)}
        result = select_topk(
            "q", data, 2, ThresholdConfig(strategy=STRICT), backend
        )
        tiny = next(item for item in result.selected if item.doc_id == "tiny")
        self.assertTrue(tiny.protected_subnormal)
        self.assertEqual(tiny.score, 0.0)


if __name__ == "__main__":
    unittest.main()
