import unittest
from dataclasses import replace

import numpy as np

from gbtdiag import TrainConfig, make_regression_dataset
from gbtdiag.binning import build_binnings
from gbtdiag.gain import scan_splits


class GainStrategyTests(unittest.TestCase):
    def setUp(self):
        self.config = TrainConfig(
            n_thresholds=6, min_leaf_samples=4, min_gain=0.0, seed=5
        )
        self.dataset = make_regression_dataset(n_rows=160, seed=5)
        self.binnings = build_binnings(
            self.dataset.X,
            self.dataset.categorical_mask,
            self.dataset.category_cardinalities,
            self.config,
        )

    def _decision(self, config):
        rows = np.arange(self.dataset.n_rows)
        gradient = self.dataset.y - self.dataset.y.mean()
        hessian = np.ones_like(gradient)
        active = list(range(self.dataset.n_features))
        return scan_splits(
            self.dataset.X,
            gradient,
            hessian,
            rows,
            self.binnings.feature_binnings,
            active,
            config,
            "root",
        )

    def test_strict_strategy_records_threshold_comparison(self):
        decision = self._decision(self.config)
        threshold_comparisons = [
            c for c in decision.comparisons if c["right"]["candidate"] == "min_gain"
        ]
        self.assertTrue(threshold_comparisons)
        self.assertEqual(threshold_comparisons[0]["strategy"], "strict")
        self.assertTrue(decision.accepted)

    def test_tolerance_strategy_accepts_candidate_on_boundary(self):
        # Pin min_gain exactly at the best gain minus a tiny epsilon so the
        # strict strategy rejects but the tolerance band still accepts.
        baseline = self._decision(self.config)
        boundary_gain = baseline.best_gain
        strict_config = replace(
            self.config,
            gain_strategy="strict",
            min_gain=boundary_gain + 1e-12,
        )
        tolerant_config = replace(
            strict_config,
            gain_strategy="tolerance",
            gain_tol=1e-9,
        )
        strict_decision = self._decision(strict_config)
        tolerant_decision = self._decision(tolerant_config)
        self.assertFalse(strict_decision.accepted)
        self.assertTrue(tolerant_decision.accepted)

    def test_comparison_records_contain_both_candidates_and_difference(self):
        decision = self._decision(self.config)
        pair_comparisons = [
            c
            for c in decision.comparisons
            if isinstance(c["left"].get("candidate"), dict)
            and isinstance(c["right"].get("candidate"), dict)
        ]
        self.assertTrue(pair_comparisons)
        for comparison in pair_comparisons:
            self.assertIn("difference", comparison)
            self.assertIn("feature_index", comparison["left"]["candidate"])
            self.assertIn("feature_index", comparison["right"]["candidate"])

    def test_tie_break_keeps_lower_feature_then_threshold(self):
        decision_a = self._decision(self.config)
        decision_b = self._decision(self.config)
        self.assertEqual(
            (decision_a.feature_index, decision_a.threshold),
            (decision_b.feature_index, decision_b.threshold),
        )


if __name__ == "__main__":
    unittest.main()
