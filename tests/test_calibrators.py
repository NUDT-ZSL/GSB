"""Requirement 2 & 3: deterministic fitters, traces, metric recomputation."""

from __future__ import annotations

import unittest

from calib_workbench.calibrators.core import (
    apply_histogram,
    apply_isotonic,
    apply_platt,
    fit_histogram,
    fit_isotonic,
    fit_platt,
)
from calib_workbench.metrics import compute_metrics
from calib_workbench.util import canonical_json


def _data():
    scores = [0.9, 0.1, 0.8, 0.35, 0.65, 0.2, 0.55, 0.42, 0.71, 0.05]
    y = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]
    ids = [f"S{i:03d}" for i in range(len(scores))]
    return scores, y, ids


class TestPlatt(unittest.TestCase):
    def test_trace_and_determinism(self):
        scores, y, ids = _data()
        params = {"max_iter": 50, "tol": 1e-10, "l2": 1e-6}
        m1, t1 = fit_platt(scores, y, ids, params)
        m2, t2 = fit_platt(scores, y, ids, params)
        self.assertEqual(canonical_json(m1), canonical_json(m2))
        self.assertEqual(canonical_json(t1), canonical_json(t2))
        self.assertGreater(len(t1["iterations"]), 1)
        self.assertEqual(t1["meta"]["init"], {"a": 0.0, "b": 0.0})
        objectives = [it["objective"] for it in t1["iterations"]]
        self.assertTrue(all(b <= a + 1e-9 for a, b in zip(objectives, objectives[1:])))
        probs = apply_platt(m1, scores)
        self.assertTrue(all(0.0 < p < 1.0 for p in probs))

    def test_tie_break_by_sample_id(self):
        scores = [0.5, 0.5, 0.5]
        y = [0, 1, 0]
        ids = ["S003", "S001", "S002"]
        m1, _ = fit_platt(scores, y, ids, {})
        m2, _ = fit_platt(scores, y, list(reversed(ids)), {})
        self.assertEqual(canonical_json(m1), canonical_json(m2))


class TestIsotonic(unittest.TestCase):
    def test_monotone_blocks_and_determinism(self):
        scores, y, ids = _data()
        m1, t1 = fit_isotonic(scores, y, ids, {})
        m2, t2 = fit_isotonic(scores, y, ids, {})
        self.assertEqual(canonical_json(m1), canonical_json(m2))
        self.assertEqual(canonical_json(t1), canonical_json(t2))
        values = [b["value"] for b in m1["blocks"]]
        self.assertEqual(values, sorted(values))
        probs = apply_isotonic(m1, sorted(scores))
        self.assertEqual(probs, sorted(probs))
        self.assertTrue(all(it["comparisons"] > 0 for it in t1["iterations"]))


class TestHistogram(unittest.TestCase):
    def test_bins_and_empty_fallback(self):
        scores, y, ids = _data()
        m1, t1 = fit_histogram(scores, y, ids, {"n_bins": 4})
        m2, _ = fit_histogram(scores, y, ids, {"n_bins": 4})
        self.assertEqual(canonical_json(m1), canonical_json(m2))
        self.assertEqual(len(m1["values"]), 4)
        self.assertEqual(sum(m1["counts"]), len(scores))
        prevalence = sum(y) / len(y)
        for b, c in enumerate(m1["counts"]):
            if c == 0:
                self.assertEqual(m1["values"][b], prevalence)
        probs = apply_histogram(m1, scores)
        self.assertTrue(all(0.0 < p < 1.0 for p in probs))
        self.assertEqual(t1["iterations"][0]["comparisons"], len(scores) + 4)


class TestMetricsRecompute(unittest.TestCase):
    def test_metrics_recomputable_from_detail(self):
        scores, y, _ = _data()
        m, _ = fit_platt(scores, y, [f"S{i:03d}" for i in range(len(scores))], {})
        probs = apply_platt(m, scores)
        bundle = compute_metrics(probs, y, 5)
        again = compute_metrics(probs, y, 5)
        self.assertEqual(canonical_json(bundle), canonical_json(again))
        curve = bundle["reliability"]
        total_w = sum(b["weight"] for b in curve["bins"])
        self.assertAlmostEqual(total_w, 1.0)
        ece = sum(b["weight"] * abs(b["mean_pred"] - b["mean_true"])
                  for b in curve["bins"] if b["count"])
        self.assertEqual(ece, bundle["ece"])
        self.assertEqual(bundle["conventions"]["ece_weight"], "bin_count/N")


if __name__ == "__main__":
    unittest.main()
