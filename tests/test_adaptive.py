from __future__ import annotations

import unittest

from voice_dialog.adaptive import select_strategy

from tests.helpers import TempCase


class AdaptiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = TempCase()

    def tearDown(self) -> None:
        self.case.close()

    def test_tier_changes_strategy(self) -> None:
        selections = []
        for tier in ("small", "medium", "large"):
            config = self.case.config(scale_tier=tier, classifier="auto", fusion={"window_mode": "adaptive"})
            decision = select_strategy(
                segment_count=20 if tier == "small" else 220,
                valid_durations=[0.8] * 20,
                turn_count=5 if tier == "small" else 20,
                granularity="fine",
                config=config,
            )
            self.assertEqual(decision["metrics"]["tier"], tier)
            self.assertTrue(decision["reason"])
            self.assertGreaterEqual(len(decision["candidates"]), 3)
            self.assertTrue(decision["selected"]["selected"])
            selections.append(decision["selected"]["strategy_id"])
        self.assertEqual(len(set(selections)), 3)

    def test_auto_tier_uses_metrics_and_is_reproducible(self) -> None:
        config = self.case.config(
            scale_tier="auto",
            scale_tiers={"small_max_units": 90, "medium_max_units": 500, "duration_weight": 10},
        )
        args = dict(
            segment_count=400,
            valid_durations=[1.0] * 400,
            turn_count=80,
            granularity="coarse",
            config=config,
        )
        first = select_strategy(**args)
        second = select_strategy(**args)
        self.assertEqual(first, second)
        self.assertEqual(first["metrics"]["tier"], "large")
        self.assertGreater(first["metrics"]["volume_units"], 500)

    def test_dimension_limit_and_override(self) -> None:
        config = self.case.config(
            scale_tier="medium",
            classifier="weighted_logistic_regression",
            fusion={"window_mode": "fixed", "window": 2},
            feature_dimension_limit=5,
        )
        decision = select_strategy(
            segment_count=200,
            valid_durations=[0.8] * 200,
            turn_count=20,
            granularity="fine",
            config=config,
        )
        self.assertEqual(len(decision["effective"]["feature_names"]), 5)
        self.assertEqual(decision["effective"]["classifier"], "weighted_logistic_regression")
        self.assertEqual(decision["effective"]["fusion_window"], 2)
        self.assertTrue(decision["constraints"]["truncated"])


if __name__ == "__main__":
    unittest.main()
