import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from h2workbench.manifest import load_manifest
from h2workbench.policy import STRATEGY_PRIORITY, select_strategy

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "samples")


def load(name):
    return load_manifest(os.path.join(SAMPLES, f"manifest_{name}.json"))


class TestPolicy(unittest.TestCase):
    def test_selection_is_deterministic(self):
        manifest = load("large")
        decisions = [select_strategy(manifest)[0]["selected"]
                     for _ in range(5)]
        self.assertEqual(len(set(decisions)), 1)

    def test_reason_contains_scale_parameters(self):
        manifest = load("medium")
        decision, _ = select_strategy(manifest)
        reason = decision["reason"]
        self.assertIn("streams=6", reason)
        self.assertIn("weight_spread=", reason)
        self.assertIn("tie_threshold=", reason)
        self.assertEqual(decision["scale_parameters"]["stream_count"], 6)

    def test_forced_strategy(self):
        manifest = load("small")
        decision, plans = select_strategy(manifest, forced="arrival")
        self.assertEqual(decision["selected"], "arrival")
        self.assertIn("forced", decision["reason"])
        self.assertIn("arrival", plans)

    def test_tie_breaks_by_stable_priority(self):
        manifest = load("large")
        # Huge threshold: every candidate is within the tie band, so the
        # first entry of the fixed priority order must win.
        manifest["policy"]["tie_threshold_ms"] = 10**6
        decision, _ = select_strategy(manifest)
        self.assertEqual(decision["selected"], STRATEGY_PRIORITY[0])
        self.assertIn("tie threshold", decision["reason"])

    def test_zero_threshold_picks_cheapest(self):
        manifest = load("large")
        manifest["policy"]["tie_threshold_ms"] = 0
        decision, _ = select_strategy(manifest)
        costs = decision["estimated_stall_costs_ms"]
        self.assertEqual(costs[decision["selected"]], min(costs.values()))

    def test_selection_recorded_with_plan(self):
        manifest = load("medium")
        decision, plans = select_strategy(manifest)
        selected_plan = plans[decision["selected"]]
        self.assertEqual(selected_plan.strategy, decision["selected"])


if __name__ == "__main__":
    unittest.main()
