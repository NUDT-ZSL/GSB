from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from elasticctl.aggregation import aggregate_metric
from elasticctl.errors import BusinessError
from elasticctl.model import parse_metrics, parse_samples
from elasticctl.service import decide, status

from tests.common import load_fixture, sample_payload


class AggregationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.metrics = parse_metrics(sample_payload("metric_sources.json"))
        cls.samples = parse_samples(
            sample_payload("metric_stream.json"),
            cls.metrics,
        )

    def test_window_aggregation_fixed_and_float_paths_agree(self) -> None:
        for timestamp in range(0, 600001, 15000):
            for metric in self.metrics.values():
                result = aggregate_metric(metric, self.samples, timestamp)
                self.assertTrue(
                    result.paths_agree,
                    (
                        metric.id,
                        timestamp,
                        result.fixed_text,
                        result.float_text,
                    ),
                )
                self.assertEqual(result.fixed_text, result.float_text)
                self.assertEqual(result.to_json()["fixedPoint"]["scale"], 1_000_000)

    def test_known_average_and_percentile_values(self) -> None:
        average = aggregate_metric(
            self.metrics["ms-api-cpu"],
            self.samples,
            120000,
        )
        percentile = aggregate_metric(
            self.metrics["ms-api-latency"],
            self.samples,
            120000,
        )
        self.assertEqual(average.fixed_text, "76.750000")
        self.assertEqual(average.sample_count, 2)
        self.assertEqual(percentile.fixed_text, "385.500000")
        self.assertEqual(percentile.sample_count, 4)
        self.assertIn("linear interpolation", percentile.interpolation)

    def test_window_boundaries_only_use_injected_clock(self) -> None:
        result = aggregate_metric(
            self.metrics["ms-api-cpu"],
            self.samples,
            120000,
        )
        self.assertEqual(result.window_start_exclusive_ms, 90000)
        self.assertEqual(result.window_end_ms, 120000)
        self.assertEqual(result.sample_count, 2)

    def test_clock_rollback_beyond_tolerance_leaves_state_unchanged(self) -> None:
        with tempfile.TemporaryDirectory(prefix="elastic-clock-") as temp:
            db_file = Path(temp) / "clock.db"
            load_fixture(db_file)
            decide(db_file, at=120000)
            before = status(db_file)
            with self.assertRaises(BusinessError) as context:
                decide(db_file, at=100000)
            self.assertEqual(context.exception.code, "E_CLOCK_ROLLBACK")
            after = status(db_file)
            self.assertEqual(before, after)

    def test_small_clock_rollback_does_not_lower_watermark(self) -> None:
        with tempfile.TemporaryDirectory(prefix="elastic-clock-tolerance-") as temp:
            db_file = Path(temp) / "clock.db"
            load_fixture(db_file)
            decide(db_file, at=120000)
            decide(db_file, at=119900)
            snapshot = status(db_file)
            self.assertEqual(snapshot["logicalClockMs"], 120000)
            self.assertEqual(snapshot["sampleWatermarkMs"], 120000)


if __name__ == "__main__":
    unittest.main()

