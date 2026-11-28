from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from aqua_edge.analysis import analyze, build_suggestions, compare_analyses
from aqua_edge.errors import AquaError
from aqua_edge.generator import (
    generate_boundary_project,
    generate_invalid_examples,
    generate_project,
)
from aqua_edge.model import parse_project
from aqua_edge.policy import apply_policy
from aqua_edge.reference import ReferenceScheduler
from aqua_edge.scheduler import Scheduler
from aqua_edge.snapshot import load_snapshot, save_snapshot
from aqua_edge.trace import compare_frame_traces


def base_project(
    streams,
    *,
    uplink=1000,
    rtt=0,
    max_streams=1,
    consumer_rate=1_000_000,
    consumer_events=None,
    outages=None,
):
    return parse_project(
        {
            "format": "aqua-edge-project",
            "project_version": 1,
            "name": "test",
            "network": {
                "uplink_bytes_per_s": uplink,
                "rtt_ms": rtt,
                "max_concurrent_streams": max_streams,
                "frame_size_limit_bytes": 10000,
            },
            "consumer": {
                "read_bytes_per_s": consumer_rate,
                "events": consumer_events or [],
            },
            "outages": outages or [],
            "streams": streams,
        }
    )


def sensor_stream(stream_id, priority, window, frames, pond="P01"):
    return {
        "stream_id": stream_id,
        "pond_id": pond,
        "kind": "sensor",
        "sensor_type": "dissolved_oxygen",
        "priority": priority,
        "window_bytes": window,
        "frames": frames,
    }


class LoaderTests(unittest.TestCase):
    def test_rejects_required_business_codes(self):
        for name, code in [
            ("invalid_order", "E_ORDER"),
            ("invalid_duplicate_stream", "E_DUPSTREAM"),
            ("invalid_frame_length", "E_FRAMELEN"),
        ]:
            with self.subTest(name=name):
                with self.assertRaises(AquaError) as caught:
                    parse_project(generate_invalid_examples()[name])
                self.assertEqual(caught.exception.code, code)

    def test_flat_frame_sequence_is_grouped_in_stream_order(self):
        document = {
            "format": "aqua-edge-project",
            "project_version": 1,
            "name": "flat",
            "network": {
                "uplink_bytes_per_s": 1000,
                "rtt_ms": 0,
                "max_concurrent_streams": 2,
                "frame_size_limit_bytes": 1000,
            },
            "consumer": {"read_bytes_per_s": 1000, "events": []},
            "outages": [],
            "streams": [
                {
                    "stream_id": "A",
                    "pond_id": "P",
                    "kind": "sensor",
                    "sensor_type": "do",
                    "priority": 1,
                    "window_bytes": 1000,
                },
                {
                    "stream_id": "B",
                    "pond_id": "P",
                    "kind": "sensor",
                    "sensor_type": "ph",
                    "priority": 1,
                    "window_bytes": 1000,
                },
            ],
            "frames": [
                {"stream_id": "A", "logical_time_ms": 0, "length_bytes": 10},
                {"stream_id": "B", "logical_time_ms": 1, "length_bytes": 20},
                {"stream_id": "A", "logical_time_ms": 2, "length_bytes": 30},
            ],
        }
        project = parse_project(document)
        self.assertEqual([frame.logical_time_ms for frame in project.streams[0].frames], [0, 2])
        self.assertEqual([frame.logical_time_ms for frame in project.streams[1].frames], [1])

        document["frames"][2]["logical_time_ms"] = 0
        with self.assertRaises(AquaError) as caught:
            parse_project(document)
        self.assertEqual(caught.exception.code, "E_ORDER")

    def test_frame_size_limit_is_explicit(self):
        document = generate_boundary_project()
        document["streams"][0]["frames"][0]["length_bytes"] = 1001
        with self.assertRaises(AquaError) as caught:
            parse_project(document)
        self.assertEqual(caught.exception.code, "E_FRAMELIMIT")


class SchedulerTests(unittest.TestCase):
    def test_priority_then_stream_id_breaks_same_time_tie(self):
        project = base_project(
            [
                sensor_stream(
                    "A",
                    1,
                    1000,
                    [{"frame_id": "A1", "logical_time_ms": 0, "length_bytes": 100}],
                ),
                sensor_stream(
                    "B",
                    2,
                    1000,
                    [{"frame_id": "B1", "logical_time_ms": 0, "length_bytes": 100}],
                ),
                sensor_stream(
                    "C",
                    1,
                    1000,
                    [{"frame_id": "C1", "logical_time_ms": 0, "length_bytes": 100}],
                ),
            ]
        )
        traces = {item["frame_id"]: item for item in Scheduler(project).run().frame_traces()}
        self.assertEqual(traces["B1"]["start_ms"], 0)
        self.assertEqual(traces["A1"]["start_ms"], 100)
        self.assertEqual(traces["C1"]["start_ms"], 200)
        self.assertEqual(traces["A1"]["blocking_type"], "BANDWIDTH")
        self.assertEqual(traces["A1"]["blocking_stream_id"], "B")

    def test_rtt_window_return(self):
        project = base_project(
            [
                sensor_stream(
                    "A",
                    1,
                    100,
                    [
                        {"frame_id": "A1", "logical_time_ms": 0, "length_bytes": 100},
                        {"frame_id": "A2", "logical_time_ms": 50, "length_bytes": 50},
                    ],
                )
            ],
            max_streams=2,
            rtt=100,
        )
        traces = {item["frame_id"]: item for item in Scheduler(project).run().frame_traces()}
        self.assertAlmostEqual(traces["A1"]["end_ms"], 100.0)
        self.assertAlmostEqual(traces["A2"]["start_ms"], 200.1)
        self.assertAlmostEqual(traces["A2"]["queue_delay_ms"], 150.1)
        self.assertEqual(traces["A2"]["blocking_type"], "WINDOW_RTT")

    def test_slow_consumer_window_accumulation(self):
        project = base_project(
            [
                sensor_stream(
                    "A",
                    1,
                    100,
                    [
                        {"frame_id": "A1", "logical_time_ms": 0, "length_bytes": 100},
                        {"frame_id": "A2", "logical_time_ms": 50, "length_bytes": 50},
                    ],
                )
            ],
            max_streams=2,
            rtt=100,
            consumer_rate=1000,
        )
        traces = {item["frame_id"]: item for item in Scheduler(project).run().frame_traces()}
        self.assertAlmostEqual(traces["A2"]["start_ms"], 300.0)
        self.assertEqual(traces["A2"]["blocking_type"], "CONSUMER_SLOW")
        self.assertEqual(traces["A2"]["blocking_stream_id"], "A")

    def test_outage_pauses_and_preserves_stream_order(self):
        project = base_project(
            [
                sensor_stream(
                    "A",
                    1,
                    1000,
                    [
                        {"frame_id": "A1", "logical_time_ms": 0, "length_bytes": 100},
                        {"frame_id": "A2", "logical_time_ms": 0, "length_bytes": 100},
                    ],
                )
            ],
            outages=[{"start_ms": 50, "end_ms": 150}],
        )
        traces = {item["frame_id"]: item for item in Scheduler(project).run().frame_traces()}
        self.assertAlmostEqual(traces["A1"]["end_ms"], 200.0)
        self.assertAlmostEqual(traces["A2"]["start_ms"], 200.0)
        self.assertGreaterEqual(traces["A2"]["start_ms"], 150.0)
        self.assertLess(traces["A1"]["start_ms"], traces["A2"]["start_ms"])
        self.assertAlmostEqual(traces["A1"]["outage_pause_ms"], 100.0)
        self.assertAlmostEqual(traces["A2"]["outage_wait_ms"], 100.0)

    def test_consumer_rate_drop_reclaims_window_credit_later(self):
        project = base_project(
            [
                sensor_stream(
                    "A",
                    1,
                    100,
                    [
                        {"frame_id": "A1", "logical_time_ms": 0, "length_bytes": 100},
                        {"frame_id": "A2", "logical_time_ms": 50, "length_bytes": 50},
                    ],
                )
            ],
            max_streams=2,
            consumer_rate=1000,
            consumer_events=[{"time_ms": 100, "read_bytes_per_s": 100}],
        )
        traces = {item["frame_id"]: item for item in Scheduler(project).run().frame_traces()}
        self.assertAlmostEqual(traces["A2"]["start_ms"], 1100.0)
        self.assertEqual(traces["A2"]["blocking_type"], "CONSUMER_SLOW")

    def test_independent_reference_matches_sample_and_boundary(self):
        for document in [generate_project(), generate_boundary_project()]:
            project = parse_project(document)
            actual = Scheduler(project).run().frame_traces()
            reference = ReferenceScheduler(project).run()
            comparison = compare_frame_traces(actual, reference)
            self.assertTrue(comparison["identical"], comparison)


class AnalysisAndPolicyTests(unittest.TestCase):
    def test_sample_has_timeout_and_policy_improves_distribution(self):
        before = parse_project(generate_project())
        before_stats = analyze(before, Scheduler(before).run())
        self.assertEqual(before_stats["control"]["timeout_count"], 1)
        self.assertEqual(before_stats["control"]["late_commands"][0]["frame_id"], "P01-AERATOR-005")
        self.assertEqual(before_stats["control"]["late_commands"][0]["blocking_stream_id"], "P02/HISTORY")

        order = [
            "P01/AERATOR-01",
            "P02/AERATOR-01",
            "P03/AERATOR-01",
            "P01/DO",
            "P02/DO",
            "P03/DO",
            "P01/PH",
            "P02/PH",
            "P03/PH",
            "P01/TEMP",
            "P02/TEMP",
            "P03/TEMP",
            "P02/HISTORY",
        ]
        windows = {
            "P01/DO": 6400,
            "P02/DO": 6400,
            "P03/DO": 6400,
            "P01/PH": 4800,
            "P02/PH": 4800,
            "P03/PH": 4800,
            "P01/TEMP": 4000,
            "P02/TEMP": 4000,
            "P03/TEMP": 4000,
        }
        after = apply_policy(
            before,
            max_streams=2,
            priority_order=order,
            priorities={"P02/HISTORY": 5},
            windows=windows,
        )
        after_stats = analyze(after, Scheduler(after).run())
        self.assertEqual(after_stats["control"]["timeout_count"], 0)
        self.assertLess(
            after_stats["queue_delay"]["p95_ms"],
            before_stats["queue_delay"]["p95_ms"],
        )
        comparison = compare_analyses(before, before_stats, after, after_stats)
        self.assertEqual(comparison["ponds"]["P01"]["before"]["timeout_frames"], 1)
        self.assertEqual(comparison["ponds"]["P01"]["after"]["timeout_frames"], 0)
        suggestions = build_suggestions(before, before_stats, after, after_stats)
        text = json.dumps(suggestions, ensure_ascii=False)
        self.assertIn("P02/HISTORY", text)
        self.assertIn("network.max_concurrent_streams", text)


class SnapshotTests(unittest.TestCase):
    def test_resume_is_frame_exact_for_clean_and_midoutage_cutoffs(self):
        project = parse_project(generate_project())
        full = Scheduler(project).run().frame_traces()
        for cutoff in [0, 1, 24, 50, 98]:
            with self.subTest(cutoff=cutoff):
                partial = Scheduler(project).run(cutoff)
                document = json.loads(json.dumps(partial.snapshot_document()))
                resumed = Scheduler.from_snapshot(document).resume()
                self.assertEqual(resumed.frame_traces(), full)
                comparison = compare_frame_traces(resumed.frame_traces(), full)
                self.assertTrue(comparison["identical"], comparison)

    def test_snapshot_file_roundtrip_and_version_errors(self):
        project = parse_project(generate_project())
        partial = Scheduler(project).run(24)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            save_snapshot(partial, path)
            scheduler, raw = load_snapshot(path)
            self.assertEqual(raw["snapshot_version"], "aqua-edge-snapshot/1")
            scheduler.resume()
            self.assertEqual(len(scheduler.frame_traces()), project.total_frames)

        wrong = copy.deepcopy(partial.snapshot_document())
        wrong["snapshot_version"] = "v0"
        with self.assertRaises(AquaError) as caught:
            Scheduler.from_snapshot(wrong)
        self.assertEqual(caught.exception.code, "E_SNAPVERSION")
        self.assertIn("aqua-edge-snapshot/1", caught.exception.message)

        missing = copy.deepcopy(partial.snapshot_document())
        del missing["state"]["credits"]
        with self.assertRaises(AquaError) as caught:
            Scheduler.from_snapshot(missing)
        self.assertEqual(caught.exception.code, "E_SNAPVERSION")

    def test_fixed_seed_generator_is_deterministic(self):
        first = generate_project()
        second = generate_project()
        self.assertEqual(first, second)
        self.assertEqual(sum(len(stream["frames"]) for stream in first["streams"]), 98)


if __name__ == "__main__":
    unittest.main()
