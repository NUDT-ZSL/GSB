from __future__ import annotations

import unittest

import numpy as np

from qos_pipeline.engine import Engine
from qos_pipeline.fixed import conversion_ledger
from qos_pipeline.metrics import derive_metrics
from qos_pipeline.sample import load_sample

from .helpers import ROOT


class MetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sample = load_sample(ROOT / "samples" / "manifest.json")
        cls.payloads, cls.state = Engine(cls.sample).replay()
        cls.metrics = derive_metrics(cls.sample, cls.payloads, cls.state)

    def test_streaming_and_truth_estimators_are_consistent(self) -> None:
        comparison = self.metrics["estimator_comparison"]
        self.assertTrue(comparison["consistent"])
        self.assertIsNone(comparison["first_divergence"])
        self.assertGreater(comparison["comparisons"], 0)

    def test_conversion_ledger_is_repeatable(self) -> None:
        first = conversion_ledger(self.sample)
        second = conversion_ledger(self.sample)
        self.assertEqual(first, second)
        self.assertTrue(
            any(row["rate_remainder"] for row in first),
            "sample must exercise non-zero fixed-point remainders",
        )

    def test_error_chain_contains_classes_events_and_conversion_steps(self) -> None:
        markers = self.metrics["precision_sensitive"]
        self.assertTrue(markers)
        for marker in markers:
            self.assertTrue(marker["participants"])
            self.assertTrue(marker["error_sources"])
            self.assertTrue(
                any(source["field"] for source in marker["error_sources"])
            )
        throughput_markers = [
            item for item in markers if item["kind"] == "observed_throughput"
        ]
        self.assertTrue(throughput_markers)
        self.assertTrue(
            any(item["event_sequences"] for item in throughput_markers)
        )

    def test_numpy_full_recalculation_matches_window_truth(self) -> None:
        window_end = self.metrics["window"]["end_ms"]
        window_start = self.metrics["window"]["start_ms"]
        for class_name in ("interactive", "bulk", "telemetry"):
            sizes = np.array(
                [
                    item["bytes"]
                    for payload in self.payloads
                    for item in payload["passes"]
                    if item["class"] == class_name
                    and window_start <= item["pass_ms"] < window_end
                ],
                dtype=np.int64,
            )
            expected = (
                int(sizes.sum()) * 8 * 1000 / self.sample.window_ms
                if sizes.size
                else 0.0
            )
            actual = float(
                self.metrics["classes"][class_name]["observed_throughput_bps"]
            )
            self.assertAlmostEqual(actual, expected, places=5)

    def test_achievable_throughput_matches_bottleneck_pir(self) -> None:
        for class_config in self.sample.classes:
            path = self.sample.path(class_config.name)
            expected = min(self.sample.class_map[item].pir_bps for item in path)
            actual = self.metrics["classes"][class_config.name][
                "achievable_throughput_bps"
            ]
            self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()

