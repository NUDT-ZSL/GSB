import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from h2workbench.fairness import (STARVATION_ROUND_THRESHOLD,
                                  diagnose_fairness)
from h2workbench.manifest import load_manifest
from h2workbench.planner import simulate

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "samples")


def load(name):
    return load_manifest(os.path.join(SAMPLES, f"manifest_{name}.json"))


class TestFairness(unittest.TestCase):
    def test_rows_sorted_and_complete(self):
        manifest = load("medium")
        plan = simulate(manifest, "weighted_rr")
        report = diagnose_fairness(manifest, plan)
        ids = [row["stream_id"] for row in report["streams"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(manifest["streams"]))
        for row in report["streams"]:
            self.assertEqual(
                round(row["actual_bytes"] - row["expected_bytes"], 3),
                row["deviation_bytes"])

    def test_actual_bytes_match_plan(self):
        manifest = load("small")
        plan = simulate(manifest, "arrival")
        report = diagnose_fairness(manifest, plan)
        for row in report["streams"]:
            expected_total = next(s["body_bytes"] for s in
                                  manifest["streams"]
                                  if s["id"] == row["stream_id"])
            self.assertEqual(row["actual_bytes"], expected_total)

    def test_starvation_flag(self):
        manifest = load("large")
        plan = simulate(manifest, "weighted_rr")
        report = diagnose_fairness(manifest, plan)
        for row in report["streams"]:
            self.assertEqual(
                row["starved"],
                row["max_starved_rounds"] >= STARVATION_ROUND_THRESHOLD)

    def test_weight_ineffective_annotation(self):
        manifest = load("medium")  # has dependency chains
        plan = simulate(manifest, "arrival")
        report = diagnose_fairness(manifest, plan)
        annotated = {row["stream_id"]: row["annotations"]
                     for row in report["streams"]}
        # Streams 3 and 4 depend on 2 and are eligible while 2 is active.
        self.assertIn("weight_ineffective_fallback", annotated[3])
        self.assertIn("weight_ineffective_fallback", annotated[4])
        for row in report["streams"]:
            if "weight_ineffective_fallback" in row["annotations"]:
                self.assertEqual(row["effective_weight"], 1)

    def test_zero_weight_fallback_defensive(self):
        manifest = load("small")
        for stream in manifest["streams"]:
            stream["weight"] = 0  # bypass manifest validation on purpose
        plan = simulate(manifest, "arrival")
        report = diagnose_fairness(manifest, plan)
        for row in report["streams"]:
            self.assertEqual(row["effective_weight"], 1)
            self.assertIn("weight_fallback", row["annotations"])

    def test_stable_output(self):
        manifest = load("large")
        plan = simulate(manifest, "window_first")
        first = diagnose_fairness(manifest, plan)
        second = diagnose_fairness(manifest, plan)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
