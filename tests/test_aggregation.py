from __future__ import annotations

import unittest
from decimal import Decimal

from elastic_control.aggregation import aggregate_windows, parse_samples
from elastic_control.errors import CLOCK_ROLLBACK, DUPLICATE_SAMPLE_ID, ControlError
from elastic_control.models import MetricSource


def metric(name: str, aggregation: str = "average", **overrides) -> MetricSource:
    payload = {
        "name": name,
        "aggregation": aggregation,
        "window_ms": 1000,
        "target_value": 1,
        "alert_threshold": 1,
        "scale": 1000,
        "error_budget": 0.01,
    }
    payload.update(overrides)
    return MetricSource.from_dict(payload)


class AggregationTests(unittest.TestCase):
    def test_window_boundaries_and_average_are_exact(self) -> None:
        samples = parse_samples(
            {
                "samples": [
                    {"sample_id": "a", "metric": "m", "timestamp_ms": 0, "value": 1},
                    {"sample_id": "b", "metric": "m", "timestamp_ms": 999, "value": 3},
                    {"sample_id": "c", "metric": "m", "timestamp_ms": 1000, "value": 10},
                    {"sample_id": "d", "metric": "m", "timestamp_ms": 2000, "value": 99},
                ]
            }
        )
        windows = aggregate_windows({"m": metric("m")}, samples, clock_ms=2000)
        self.assertEqual([item.window_start_ms for item in windows], [0, 1000])
        self.assertEqual(windows[0].aggregate, Decimal(2))
        self.assertEqual(windows[0].sample_ids, ("a", "b"))
        self.assertEqual(windows[1].aggregate, Decimal(10))
        repeated = aggregate_windows({"m": metric("m")}, samples, clock_ms=2000)
        self.assertEqual(
            [item.to_dict() for item in windows],
            [item.to_dict() for item in repeated],
        )

    def test_percentile_uses_fixed_sort_and_linear_interpolation(self) -> None:
        samples = parse_samples(
            {
                "samples": [
                    {"sample_id": "d", "metric": "p", "timestamp_ms": 1, "value": 40},
                    {"sample_id": "b", "metric": "p", "timestamp_ms": 2, "value": 20},
                    {"sample_id": "a", "metric": "p", "timestamp_ms": 3, "value": 10},
                    {"sample_id": "c", "metric": "p", "timestamp_ms": 4, "value": 30},
                ]
            }
        )
        source = metric("p", aggregation="percentile", percentile=95)
        window = aggregate_windows({"p": source}, samples, clock_ms=1000)[0]
        self.assertEqual(window.aggregate, Decimal("38.5"))
        self.assertEqual(window.sample_ids, ("d", "b", "a", "c"))

    def test_exact_duplicate_is_counted_once_and_conflict_is_rejected(self) -> None:
        samples = parse_samples(
            {
                "samples": [
                    {"sample_id": "x", "metric": "m", "timestamp_ms": 1, "value": 2},
                    {"sample_id": "x", "metric": "m", "timestamp_ms": 1, "value": 2},
                ]
            }
        )
        self.assertEqual(len(samples), 1)
        with self.assertRaises(ControlError) as caught:
            parse_samples(
                {
                    "samples": [
                        {"sample_id": "x", "metric": "m", "timestamp_ms": 1, "value": 2},
                        {"sample_id": "x", "metric": "m", "timestamp_ms": 1, "value": 3},
                    ]
                }
            )
        self.assertEqual(caught.exception.code, DUPLICATE_SAMPLE_ID)

    def test_large_clock_rollback_is_rejected(self) -> None:
        samples = parse_samples(
            {
                "samples": [
                    {"sample_id": "a", "metric": "m", "timestamp_ms": 1000, "value": 1},
                    {"sample_id": "b", "metric": "m", "timestamp_ms": 800, "value": 1},
                ]
            }
        )
        with self.assertRaises(ControlError) as caught:
            aggregate_windows(
                {"m": metric("m")},
                samples,
                clock_ms=2000,
                rollback_tolerance_ms=50,
            )
        self.assertEqual(caught.exception.code, CLOCK_ROLLBACK)


if __name__ == "__main__":
    unittest.main()
