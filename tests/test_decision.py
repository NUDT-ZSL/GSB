from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from elastic_control.aggregation import aggregate_windows
from elastic_control.decision import decide
from elastic_control.engine import load_sample_file
from elastic_control.fixedpoint import FixedPointBook
from elastic_control.models import load_policy_from_directory


ROOT = Path(__file__).resolve().parents[1]


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = load_policy_from_directory(ROOT / "samples")
        self.sample_bundle = load_sample_file(ROOT / "samples" / "samples.json", self.bundle)
        self.windows = aggregate_windows(
            self.bundle.metrics,
            self.sample_bundle.samples,
            clock_ms=2000,
        )
        self.replicas = {
            name: workload.current_replicas
            for name, workload in self.bundle.workloads.items()
        }

    def test_priority_direction_rejection_and_sensitivity(self) -> None:
        result = decide(
            self.bundle,
            self.windows,
            self.replicas,
            at_ms=2000,
            ledger=FixedPointBook(),
        )
        api = result.workloads["api-web"]
        self.assertEqual(api.selected_rule, "api-cpu-scale-up")
        self.assertEqual(api.target_replicas, 5)
        rejected = {
            item["rule"]: item["first_rejection_reason"]
            for item in api.rejected_rules
        }
        self.assertEqual(rejected["api-latency-scale-up"], "lower_candidate_score")
        self.assertTrue(api.precision_sensitive)
        self.assertTrue(
            any(flag.flag_type == "close_candidate_scores" for flag in result.precision_flags)
        )
        flag = next(
            item for item in result.precision_flags if item.flag_type == "close_candidate_scores"
        )
        self.assertIsNotNone(flag.would_flip_at_higher_scale)
        self.assertGreater(flag.higher_scale or 0, 0)
        self.assertEqual(result.workloads["batch"].target_replicas, 4)
        self.assertEqual(result.workloads["archive"].target_replicas, 8)

    def test_precision_divergence_is_reported_first(self) -> None:
        assert self.bundle.budget is not None
        strict_bundle = replace(
            self.bundle,
            budget=replace(
                self.bundle.budget,
                float_tolerance=Decimal("0.000001"),
            ),
        )
        result = decide(
            strict_bundle,
            self.windows,
            self.replicas,
            at_ms=2000,
            ledger=FixedPointBook(),
        )
        self.assertIsNotNone(result.first_divergence)
        assert result.first_divergence is not None
        self.assertTrue(result.first_divergence.metric)
        self.assertIsNotNone(result.first_divergence.fixed_value)
        self.assertIsNotNone(result.first_divergence.decimal_value)
        self.assertTrue(result.first_divergence.step_id)
        self.assertTrue(
            any(
                flag.flag_type == "fixed_decimal_divergence"
                for flag in result.precision_flags
            )
        )


if __name__ == "__main__":
    unittest.main()
