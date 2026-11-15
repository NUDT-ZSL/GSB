from __future__ import annotations

import math
import random
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sensor_stream_agg import Aggregator, AggregatorConfig, ErrorCode, Event


def event_values() -> list[Event]:
    rng = random.Random(20261115)
    events: list[Event] = []
    for index in range(28):
        event_time = 1_000_000 + index * 37_000 + rng.randrange(-9_000, 9_001)
        magnitude = 10.0 ** rng.uniform(-5.0, 9.0)
        sign = -1.0 if index % 7 in (2, 5) else 1.0
        observation = sign * magnitude
        weight = 10.0 ** rng.uniform(-2.0, 1.0)
        events.append(
            Event(
                event_time_us=event_time,
                device_id=index % 4,
                sequence=index,
                observation=observation,
                weight=weight,
            )
        )
    return events


def hex_result(result: object) -> tuple[str, ...]:
    return (
        float(result.sum_value).hex(),
        float(result.rms_value).hex(),
        float(result.weighted_mean).hex(),
        float(result.sum_total).hex(),
        float(result.sum_compensation).hex(),
        float(result.sum_absolute).hex(),
        float(result.weighted_sum_total).hex(),
        float(result.weighted_sum_compensation).hex(),
        float(result.weight_total).hex(),
        float(result.square_sum_total).hex(),
        float(result.square_sum_compensation).hex(),
    )


class AggregationTests(unittest.TestCase):
    def test_invalid_window_parameters(self) -> None:
        with self.assertRaisesRegex(Exception, "E_WINDOW_PARAM"):
            AggregatorConfig(window_length_us=0)
        with self.assertRaisesRegex(Exception, "E_WINDOW_PARAM"):
            AggregatorConfig(strategy="sliding", window_length_us=100, slide_us=101)
        with self.assertRaisesRegex(Exception, "E_WINDOW_PARAM"):
            AggregatorConfig(allowed_lateness_us=-1)

    def test_order_invariance_fixed(self) -> None:
        events = event_values()
        config = AggregatorConfig(
            strategy="fixed",
            window_length_us=120_000,
            allowed_lateness_us=10**12,
        )
        permutations: list[list[Event]] = [list(events), list(reversed(events))]
        for seed in range(4):
            shuffled = list(events)
            random.Random(seed).shuffle(shuffled)
            permutations.append(shuffled)

        baselines: dict[tuple[int, int], tuple[str, ...]] = {}
        with tempfile.TemporaryDirectory() as directory:
            for run_index, ordered in enumerate(permutations):
                db_path = Path(directory) / f"fixed_{run_index}.sqlite3"
                with Aggregator(db_path, config) as aggregator:
                    for event in ordered:
                        result = aggregator.ingest(event)
                        self.assertEqual(result.code, ErrorCode.OK.value)
                        self.assertTrue(result.accepted)
                    aggregator.flush()
                    windows = aggregator.list_windows("closed")
                    self.assertTrue(windows)
                    current = {
                        (window.start_us, window.end_us): hex_result(window)
                        for window in windows
                    }
                    if not baselines:
                        baselines = current
                    else:
                        self.assertEqual(current, baselines)
                    for window in windows:
                        report = aggregator.error_report(window.window_id)
                        self.assertEqual(report.code, ErrorCode.OK.value)
                        self.assertLessEqual(report.sum_deviation, window.sum_error_bound)
                        self.assertLessEqual(report.rms_deviation, window.rms_error_bound)
                        self.assertLessEqual(
                            report.weighted_mean_deviation,
                            window.weighted_mean_error_bound,
                        )

    def test_order_invariance_sliding(self) -> None:
        events = event_values()[:18]
        config = AggregatorConfig(
            strategy="sliding",
            window_length_us=150_000,
            slide_us=30_000,
            allowed_lateness_us=10**12,
        )
        baseline: dict[tuple[int, int], tuple[str, ...]] = {}
        with tempfile.TemporaryDirectory() as directory:
            for run_index, ordered in enumerate(
                (list(events), list(reversed(events)))
            ):
                with Aggregator(Path(directory) / f"sliding_{run_index}.db", config) as agg:
                    for event in ordered:
                        self.assertEqual(agg.ingest(event).code, ErrorCode.OK.value)
                    agg.flush()
                    current = {
                        (window.start_us, window.end_us): hex_result(window)
                        for window in agg.list_windows("closed")
                    }
                    self.assertGreater(len(current), 18)
                    if not baseline:
                        baseline = current
                    else:
                        self.assertEqual(current, baseline)

    def test_sliding_partial_late_updates_only_open_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AggregatorConfig(
                strategy="sliding",
                window_length_us=100,
                slide_us=50,
                allowed_lateness_us=20,
            )
            with Aggregator(Path(directory) / "partial.db", config) as aggregator:
                aggregator.ingest(Event(10, 1, 1, 1.0))
                aggregator.ingest(Event(130, 1, 2, 2.0))
                result = aggregator.ingest(Event(60, 1, 3, 3.0))
                self.assertEqual(result.code, ErrorCode.OK.value)
                self.assertEqual(result.status, "PARTIAL_LATE")
                self.assertTrue(result.late)
                self.assertEqual(result.dropped_windows, ((0, 100),))
                self.assertEqual(len(result.window_ids), 1)
                self.assertEqual(result.steps[0].member_count, 2)
                dropped = aggregator.summary()
                self.assertEqual(dropped["events_partial_late"], 1)
                self.assertEqual(dropped["late_drop_log_entries"], 1)

    def test_late_event_and_closed_window_immutability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "late.db"
            config = AggregatorConfig(
                strategy="fixed",
                window_length_us=100,
                allowed_lateness_us=20,
            )
            with Aggregator(db_path, config) as aggregator:
                for event in (
                    Event(10, 1, 1, 1.0e9),
                    Event(20, 1, 2, 1.0e-9),
                    Event(90, 1, 3, 3.0),
                ):
                    self.assertEqual(aggregator.ingest(event).code, "OK")

                late = aggregator.ingest(Event(15, 1, 4, -2.0))
                self.assertEqual(late.code, ErrorCode.OK.value)
                self.assertTrue(late.late)
                self.assertEqual(late.status, "ACCEPTED_LATE")
                self.assertEqual(late.steps[0].member_count, 4)

                advance = aggregator.ingest(Event(300, 1, 5, 4.0))
                self.assertEqual(advance.code, ErrorCode.OK.value)
                closed = aggregator.list_windows("closed")
                self.assertEqual(len(closed), 1)
                old = closed[0]
                before = hex_result(old)

                dropped = aggregator.ingest(Event(12, 1, 6, 9.0))
                self.assertEqual(dropped.code, ErrorCode.E_LATE_DROP.value)
                self.assertFalse(dropped.accepted)
                self.assertEqual(dropped.dropped_windows, ((0, 100),))
                after = aggregator.get_window(old.window_id)
                self.assertIsNotNone(after)
                assert after is not None
                self.assertEqual(hex_result(after), before)
                summary = aggregator.summary()
                self.assertEqual(summary["events_late_dropped"], 1)
                self.assertEqual(summary["late_drop_log_entries"], 1)

                transition = aggregator.transition_window(old.window_id, "open")
                self.assertEqual(transition.code, ErrorCode.E_WINDOW_STATE.value)
                open_window = aggregator.list_windows("open")[0]
                early_close = aggregator.transition_window(
                    open_window.window_id, "closed"
                )
                self.assertEqual(early_close.code, ErrorCode.E_WINDOW_STATE.value)

                duplicate = aggregator.ingest(Event(20, 1, 2, 123456.0))
                self.assertEqual(duplicate.code, ErrorCode.E_DUPLICATE.value)
                self.assertEqual(hex_result(aggregator.get_window(old.window_id)), before)

            raw = sqlite3.connect(db_path)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    raw.execute(
                        "UPDATE windows SET sum_value = 42.0 WHERE window_id = ?",
                        (old.window_id,),
                    )
                raw.rollback()
            finally:
                raw.close()

    def test_time_regression_injection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AggregatorConfig(strict_event_time_order=True)
            with Aggregator(Path(directory) / "regress.db", config) as aggregator:
                self.assertEqual(aggregator.ingest(Event(100, 1, 1, 1.0)).code, "OK")
                regressed = aggregator.ingest(Event(99, 1, 2, 2.0))
                self.assertEqual(regressed.code, ErrorCode.E_TIME_REGRESS.value)
                self.assertEqual(aggregator.summary()["events_total"], 1)

    def test_db_conflict_rolls_back_and_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "conflict.db"
            base = AggregatorConfig(window_length_us=100, allowed_lateness_us=20)
            with Aggregator(db_path, base) as aggregator:
                aggregator.ingest(Event(10, 1, 1, 1.0))
                aggregator.flush()
                closed = aggregator.list_windows("closed")[0]
                before = hex_result(closed)

            injected = AggregatorConfig(
                window_length_us=100,
                allowed_lateness_us=20,
                inject_db_conflict_once=True,
            )
            with Aggregator(db_path, injected) as aggregator:
                failed = aggregator.ingest(Event(250, 1, 2, 2.0))
                self.assertEqual(failed.code, ErrorCode.E_DB_CONFLICT.value)
                self.assertEqual(aggregator.summary()["events_total"], 1)
                self.assertEqual(
                    hex_result(aggregator.get_window(closed.window_id)), before
                )
                retried = aggregator.ingest(Event(250, 1, 2, 2.0))
                self.assertEqual(retried.code, ErrorCode.OK.value)
                self.assertEqual(aggregator.summary()["events_total"], 2)

    def test_concurrent_duplicate_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "concurrent.db"
            config = AggregatorConfig(window_length_us=1_000, allowed_lateness_us=100)
            seed = Aggregator(db_path, config)
            seed.close()
            same_event = Event(100, 7, 42, 5.0, 2.0)
            barrier = threading.Barrier(6)
            results: list[str] = []
            errors: list[BaseException] = []
            lock = threading.Lock()

            def worker() -> None:
                try:
                    aggregator = Aggregator(db_path, config)
                    barrier.wait(timeout=10)
                    result = aggregator.ingest(same_event)
                    with lock:
                        results.append(result.code)
                    aggregator.close()
                except BaseException as exc:  # pragma: no cover - assertion detail
                    with lock:
                        errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            self.assertEqual(errors, [])
            self.assertEqual(results.count(ErrorCode.OK.value), 1)
            self.assertEqual(results.count(ErrorCode.E_DUPLICATE.value), 5)

            with Aggregator(db_path, config) as aggregator:
                aggregator.flush()
                window = aggregator.list_windows("closed")[0]
                self.assertEqual(window.member_count, 1)
                self.assertEqual(window.weighted_mean, 5.0)

    def test_recovery_preserves_closed_bits_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "recover.db"
            config = AggregatorConfig(
                strategy="fixed", window_length_us=100_000, allowed_lateness_us=20_000
            )
            first = Aggregator(db_path, config)
            for index, timestamp in enumerate((1_010_000, 1_090_000, 1_250_000), 1):
                self.assertEqual(
                    first.ingest(Event(timestamp, 1, index, float(index))).code,
                    ErrorCode.OK.value,
                )
            first.flush(1_230_000)
            closed_before = first.list_windows("closed")
            self.assertTrue(closed_before)
            before = {window.window_id: hex_result(window) for window in closed_before}
            summary_before = first.summary()
            first.close()

            second = Aggregator(db_path, config)
            try:
                self.assertEqual(
                    {window.window_id: hex_result(window) for window in second.list_windows("closed")},
                    before,
                )
                self.assertEqual(second.summary()["windows_closed"], summary_before["windows_closed"])
                late_open = second.ingest(Event(1_210_000, 2, 10, 10.0))
                self.assertEqual(late_open.code, ErrorCode.OK.value)
                self.assertTrue(late_open.late)
                dropped = second.ingest(Event(1_050_000, 2, 11, 11.0))
                self.assertEqual(dropped.code, ErrorCode.E_LATE_DROP.value)
                self.assertEqual(
                    {window.window_id: hex_result(window) for window in second.list_windows("closed") if window.window_id in before},
                    before,
                )
            finally:
                second.close()

    def test_every_reported_bound_is_not_violated(self) -> None:
        events = event_values()
        with tempfile.TemporaryDirectory() as directory:
            config = AggregatorConfig(window_length_us=200_000, allowed_lateness_us=50_000)
            with Aggregator(Path(directory) / "bounds.db", config) as aggregator:
                for result in (aggregator.ingest(event) for event in events):
                    self.assertEqual(result.code, ErrorCode.OK.value)
                    for step in result.steps:
                        self.assertGreaterEqual(step.sum_error_bound, 0.0)
                        self.assertGreaterEqual(step.rms_error_bound, 0.0)
                        self.assertGreaterEqual(step.weighted_mean_error_bound, 0.0)
                aggregator.flush()
                reports = [
                    aggregator.error_report(window.window_id)
                    for window in aggregator.list_windows("closed")
                ]
                self.assertGreaterEqual(len(reports), 2)
                for report in reports:
                    self.assertEqual(report.code, ErrorCode.OK.value, report)
                    self.assertLessEqual(
                        report.sum_deviation, report.window.sum_error_bound
                    )
                    self.assertLessEqual(
                        report.rms_deviation, report.window.rms_error_bound
                    )
                    self.assertLessEqual(
                        report.weighted_mean_deviation,
                        report.window.weighted_mean_error_bound,
                    )
                    self.assertTrue(math.isfinite(report.window.sum_value))

    def test_errbound_marker_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AggregatorConfig(window_length_us=100, allowed_lateness_us=10)
            with patch(
                "sensor_stream_agg.aggregator.bound_failures",
                return_value=(0.0, 0.0, 0.0, ("sum",)),
            ):
                with Aggregator(Path(directory) / "errbound.db", config) as aggregator:
                    aggregator.ingest(Event(10, 1, 1, 1.0))
                    aggregator.flush()
                    report = aggregator.error_report(1)
                    self.assertEqual(report.code, ErrorCode.E_ERRBOUND.value)
                    self.assertEqual(
                        aggregator.summary()["error_bound_failures"], 1
                    )


if __name__ == "__main__":
    unittest.main()
