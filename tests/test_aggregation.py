from __future__ import annotations

import tempfile
import unittest
from random import Random
from pathlib import Path

from sre_burnrate import Event, FullWindowAggregator, StreamingWindowAggregator, evaluate_file

from tests.helpers import make_config, simple_records, write_ndjson


class AggregationTests(unittest.TestCase):
    def _events(self) -> list[Event]:
        return [
            Event(0, 100, 1, 1),
            Event(60, 200, 2, 2),
            Event(120, 300, 3, 3),
            Event(180, 400, 4, 4),
            Event(240, 500, 5, 5),
        ]

    def test_window_boundaries_totals_and_partial_flag(self) -> None:
        config = make_config(long_minutes=3, short_minutes=2, step_minutes=2)
        result = FullWindowAggregator(config).aggregate(self._events())
        self.assertEqual([window.start_minute for window in result.long_windows], [0, 120, 240])
        self.assertEqual(
            [
                (window.total_requests, window.failed_requests, window.partial)
                for window in result.long_windows
            ],
            [(600, 6, False), (1200, 12, False), (500, 5, True)],
        )
        self.assertEqual(
            [
                (window.total_requests, window.failed_requests, window.partial)
                for window in result.short_windows
            ],
            [(300, 3, False), (700, 7, False), (500, 5, True)],
        )
        self.assertEqual(result.long_windows[-1].coverage_minutes, 1)
        self.assertEqual(result.long_windows[-1].observed_minutes, 1)
        self.assertEqual(result.long_windows[0].sli.value, "0.990000000")
        self.assertEqual(result.long_windows[0].burn_rate.value, "10.000000")

    def test_full_and_streaming_are_bit_identical(self) -> None:
        config = make_config(long_minutes=3, short_minutes=2, step_minutes=2)
        full = FullWindowAggregator(config).aggregate(self._events())
        streaming = StreamingWindowAggregator(config)
        for event in self._events():
            streaming.feed(event)
        online = streaming.finalize()
        self.assertEqual(
            [window.to_dict() for window in full.long_windows],
            [window.to_dict() for window in online.long_windows],
        )
        self.assertEqual(
            [window.to_dict() for window in full.short_windows],
            [window.to_dict() for window in online.short_windows],
        )

    def test_gaps_overlap_and_step_larger_than_window(self) -> None:
        config = make_config(long_minutes=4, short_minutes=2, step_minutes=7)
        events = [
            Event(0, 10, 0, 1),
            Event(60, 20, 1, 2),
            Event(300, 30, 2, 3),
            Event(360, 40, 3, 4),
            Event(600, 50, 5, 5),
        ]
        full = FullWindowAggregator(config).aggregate(events)
        streaming = StreamingWindowAggregator(config)
        for event in events:
            streaming.feed(event)
        online = streaming.finalize()
        self.assertEqual(
            [window.to_dict() for window in full.long_windows],
            [window.to_dict() for window in online.long_windows],
        )
        self.assertEqual(
            [window.to_dict() for window in full.short_windows],
            [window.to_dict() for window in online.short_windows],
        )

    def test_adaptive_path_switch_preserves_results(self) -> None:
        records = simple_records(100, failure_burst=True)
        with tempfile.TemporaryDirectory() as directory:
            path = write_ndjson(Path(directory) / "data.ndjson", records)
            full = evaluate_file(path, make_config(adaptive_threshold=10_000), force_path="full")
            streaming = evaluate_file(path, make_config(adaptive_threshold=10_000), force_path="streaming")
            automatic = evaluate_file(path, make_config(adaptive_threshold=10), force_path="auto")
            small_threshold = evaluate_file(path, make_config(adaptive_threshold=10_000), force_path="auto")
        self.assertEqual(full.resource.adaptive_path, "full")
        self.assertEqual(streaming.resource.adaptive_path, "streaming")
        self.assertEqual(automatic.resource.adaptive_path, "streaming")
        self.assertEqual(small_threshold.resource.adaptive_path, "full")
        expected_long = [window.to_dict() for window in full.long_windows]
        expected_short = [window.to_dict() for window in full.short_windows]
        expected_steps = [step.to_dict() for step in full.steps]
        self.assertEqual(expected_long, [window.to_dict() for window in streaming.long_windows])
        self.assertEqual(expected_short, [window.to_dict() for window in streaming.short_windows])
        self.assertEqual(expected_steps, [step.to_dict() for step in streaming.steps])
        self.assertEqual(expected_long, [window.to_dict() for window in automatic.long_windows])
        self.assertEqual(expected_short, [window.to_dict() for window in automatic.short_windows])
        self.assertEqual(expected_steps, [step.to_dict() for step in automatic.steps])

    def test_resource_report_has_integer_units_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_ndjson(Path(directory) / "data.ndjson", simple_records(20))
            session = evaluate_file(path, make_config(adaptive_threshold=0))
        report = session.resource.to_dict()
        self.assertEqual(report["adaptive_path"], "streaming")
        self.assertGreater(report["peak_memory_estimate_bytes"], 0)
        self.assertGreater(report["peak_memory_observed_bytes"], 0)
        self.assertIsInstance(report["elapsed_ms"], int)
        self.assertIn("tracemalloc peak", report["memory_accounting"])
        self.assertIn("retained core structures", report["estimator"])

    def test_randomized_gap_equivalence_across_window_shapes(self) -> None:
        rng = Random(20261117)
        for long_minutes, short_minutes, step_minutes in (
            (60, 5, 1),
            (60, 5, 17),
            (30, 10, 60),
            (7, 1, 3),
            (5, 5, 5),
        ):
            events: list[Event] = []
            minute = 0
            for _ in range(2_000):
                minute += rng.randint(1, 3)
                total = rng.randint(0, 500)
                failed = rng.randint(0, total)
                events.append(Event(minute * 60, total, failed, len(events) + 1))
            config = make_config(
                long_minutes=long_minutes,
                short_minutes=short_minutes,
                step_minutes=step_minutes,
            )
            full = FullWindowAggregator(config).aggregate(events)
            streaming = StreamingWindowAggregator(config)
            for event in events:
                streaming.feed(event)
            online = streaming.finalize()
            with self.subTest(
                long=long_minutes, short=short_minutes, step=step_minutes
            ):
                self.assertEqual(
                    [window.to_dict() for window in full.long_windows],
                    [window.to_dict() for window in online.long_windows],
                )
                self.assertEqual(
                    [window.to_dict() for window in full.short_windows],
                    [window.to_dict() for window in online.short_windows],
                )


if __name__ == "__main__":
    unittest.main()
