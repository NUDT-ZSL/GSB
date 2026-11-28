"""Acceptance tests for the rhythm judging kernel (standard library only).

Run with: ``python3 -m unittest discover -s tests -v``
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from rhythm_kernel.engine import RhythmEngine, load_project
from rhythm_kernel.generator import generate_project
from rhythm_kernel.incremental import recompute_incremental
from rhythm_kernel.model import grade_for, median, nearest_beat, window_bounds
from rhythm_kernel.persistence import load_snapshot, save_snapshot, verify_snapshot
from rhythm_kernel.reference import compare_with_kernel, reference_judge

REFERENCE_PROJECT = os.path.join(
    os.path.dirname(__file__), "..", "artifacts", "demo_project.json"
)
REFERENCE_RESULTS = os.path.join(
    os.path.dirname(__file__), "..", "artifacts", "results.json"
)
REFERENCE_SNAPSHOT = os.path.join(
    os.path.dirname(__file__), "..", "artifacts", "snapshot.json"
)


class KernelAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = load_project(REFERENCE_PROJECT)
        cls.engine = RhythmEngine(cls.project)
        cls.report = cls.engine.run_full()
        with open(REFERENCE_RESULTS, encoding="utf-8") as handle:
            cls.reference_report = json.load(handle)

    def test_reference_judge_agrees_within_tolerance(self):
        comparison = compare_with_kernel(self.report, self.project)
        self.assertTrue(comparison["ok"], comparison["mismatches"])
        self.assertEqual(comparison["mismatch_count"], 0)

    def test_results_match_reference_field_by_field(self):
        self.assertEqual(self.report, self.reference_report)

    def test_correction_uses_half_median_rtt(self):
        for record in self.report["corrections_by_channel"]:
            self.assertAlmostEqual(
                record["one_way_correction_ms"],
                record["rtt_median_ms"] / 2.0,
            )

    def test_windows_scale_with_bpm(self):
        for result in self.report["results"]:
            judgement = result["judgment"]
            if judgement["windows_ms"] is None:
                continue
            interval = judgement["beat_interval_ms"]
            boundaries = judgement["windows_ms"]["boundaries_ms"]
            ratios = self.project["policy"]["window_ratios"]
            self.assertAlmostEqual(boundaries["perfect"], interval * ratios["perfect"])
            self.assertAlmostEqual(boundaries["miss"], interval * ratios["miss"])

    def test_validation_flags_every_invalid_event(self):
        invalid_ids = {
            result["event_id"]
            for result in self.report["results"]
            if not result["valid"]
        }
        self.assertEqual(
            invalid_ids,
            {
                "evt-invalid-late-click",
                "evt-invalid-unknown-song",
                "evt-invalid-missing-channel",
            },
        )
        codes = {issue["code"] for issue in self.report["validation"]}
        self.assertIn("event.click_out_of_legal_range", codes)
        self.assertIn("event.unknown_channel_group", codes)
        self.assertIn("event.unknown_song", codes)

    def test_trigger_ordering_and_invariants(self):
        triggers = self.report["trigger_sequence"]
        self.assertEqual(
            [trigger["sequence_index"] for trigger in triggers],
            list(range(len(triggers))),
        )
        for segment_index in range(4):
            sub = [
                trigger
                for trigger in triggers
                if trigger["segment_declared_index"] == segment_index
            ]
            keys = [
                (
                    trigger["trigger_time_ms"],
                    0 if trigger["type"] == "beat_cue" else 1,
                    self.report["event_order"].index(trigger["event_id"]),
                )
                for trigger in sub
            ]
            self.assertEqual(keys, sorted(keys))
        for trigger in triggers:
            self.assertTrue(trigger["not_before_beat"])
            self.assertTrue(trigger["within_segment"])

    def test_language_flags_match_thresholds(self):
        flagged = {
            row["language"] for row in self.report["stats"]["languages"] if row["flagged"]
        }
        self.assertEqual(flagged, {"zh"})

    def test_consistency_report_locates_outliers(self):
        report = self.report["consistency_report"]
        groups = {issue["group_key"] for issue in report["issues"] if "group_key" in issue}
        self.assertIn("ja|voice", groups)
        self.assertIn("ja|web", groups)

    def test_snapshot_hashes_match_reference(self):
        with open(REFERENCE_SNAPSHOT, encoding="utf-8") as handle:
            reference_snapshot = json.load(handle)
        for section in (
            "project_sha256",
            "stats_sha256",
            "trigger_sha256",
            "validation_sha256",
            "consistency_sha256",
        ):
            self.assertEqual(
                self._snapshot()["integrity"][section],
                reference_snapshot["integrity"][section],
            )
        self.assertEqual(
            self._snapshot()["integrity"]["result_sha256"],
            reference_snapshot["integrity"]["result_sha256"],
        )

    def _snapshot(self):
        from rhythm_kernel.persistence import build_snapshot

        return build_snapshot(self.report, self.project)


class PureRuleTest(unittest.TestCase):
    def test_median(self):
        self.assertEqual(median([3, 1, 2]), 2)
        self.assertEqual(median([4, 1, 3, 2]), 2.5)

    def test_nearest_beat_tie_prefers_earlier(self):
        beats = [0.0, 100.0, 200.0]
        index, value = nearest_beat(beats, 150.0)
        self.assertEqual((index, value), (1, 100.0))

    def test_inclusive_grade_boundaries(self):
        boundaries = window_bounds(500.0, {"perfect": 0.08, "good": 0.18,
                                           "offset": 0.45, "miss": 0.5})
        self.assertEqual(grade_for(boundaries["perfect"], boundaries), "PERFECT")
        self.assertEqual(grade_for(boundaries["good"], boundaries), "GOOD")
        self.assertEqual(grade_for(boundaries["offset"], boundaries), "OFFSET")
        self.assertEqual(grade_for(boundaries["miss"], boundaries), "MISS")


class TriggerClampTest(unittest.TestCase):
    def test_triggers_are_clamped_inside_attributed_segment(self):
        project = generate_project()
        engine = RhythmEngine(project)
        correction = engine.profiles["zh|web"]["one_way_correction_ms"]
        event = {
            "channel": "web",
            "click_ms": 239600.0 + correction,
            "id": "evt-clamp-edge",
            "language": "zh",
            "listener_id": "listener-clamp",
            "requested_song_id": "song-alpha",
            "submitted_at_ms": 239400.0,
        }
        self.assertEqual(event["click_ms"] - correction, 239600.0)
        project["events"].append(event)
        project["timeline"][-1]["end_ms"] = 239550.0

        report = RhythmEngine(project).run_full()
        result = next(
            item for item in report["results"] if item["event_id"] == event["id"]
        )
        self.assertTrue(result["valid"])
        segment = result["segment"]
        self.assertLessEqual(result["beat_ms"], segment["end_ms"])
        for trigger in result["triggers"]:
            self.assertGreaterEqual(trigger["trigger_time_ms"], trigger["beat_ms"])
            self.assertLessEqual(trigger["trigger_time_ms"], segment["end_ms"])
            self.assertTrue(trigger["within_segment"])
        cue, request = result["triggers"]
        self.assertEqual(cue["trigger_time_ms"], cue["preferred_time_ms"])
        self.assertFalse(cue["clamped_to_segment_end"])
        self.assertEqual(request["trigger_time_ms"], segment["end_ms"])
        self.assertTrue(request["clamped_to_segment_end"])


class IncrementalTest(unittest.TestCase):
    def setUp(self):
        self.project = generate_project()
        self.engine = RhythmEngine(self.project)
        self.baseline = self.engine.run_full()

    @staticmethod
    def _core(report):
        """Report content excluding recomputation bookkeeping metadata."""
        return {
            key: value
            for key, value in report.items()
            if key not in ("incremental_history", "state_revision")
        }

    def test_event_change_only_rejudges_that_event(self):
        target = self.project["events"][0]
        modified = dict(target)
        modified["click_ms"] = target["click_ms"] + 37.5
        self.engine.upsert_event(modified)
        report, scope = recompute_incremental(
            self.engine,
            self.baseline,
            changed_event_ids=[target["id"]],
            reason="unit_test_event_change",
        )
        self.assertEqual(scope["recomputed_event_ids"], [target["id"]])
        self.assertEqual(scope["unchanged_event_count"], len(self.baseline["results"]) - 1)

        before = {item["event_id"]: item for item in self.baseline["results"]}
        after = {item["event_id"]: item for item in report["results"]}
        for event_id, record in before.items():
            if event_id != target["id"]:
                self.assertEqual(record, after[event_id])

        full = RhythmEngine(self.engine.project).run_full()
        self.assertEqual(self._core(report), self._core(full))

    def test_beat_grid_change_rejudges_affected_song_only(self):
        song = self.engine.songs["song-alpha"]
        new_beats = [beat + 12.5 for beat in song["beats_ms"]]
        expected_ids = [
            event["id"]
            for event in self.project["events"]
            if event["requested_song_id"] == "song-alpha"
        ]
        self.engine.update_song_beats("song-alpha", new_beats)
        report, scope = recompute_incremental(
            self.engine,
            self.baseline,
            changed_song_ids=["song-alpha"],
            reason="unit_test_grid_change",
        )
        self.assertEqual(sorted(scope["recomputed_event_ids"]), sorted(expected_ids))
        before = {item["event_id"]: item for item in self.baseline["results"]}
        after = {item["event_id"]: item for item in report["results"]}
        for event_id, record in before.items():
            if event_id not in expected_ids:
                self.assertEqual(record, after[event_id])
        full = RhythmEngine(self.engine.project).run_full()
        self.assertEqual(self._core(report), self._core(full))
        self.assertEqual(scope["affected_beat_ranges"][0]["song_id"], "song-alpha")

    def test_new_event_is_picked_up_incrementally(self):
        new_event = dict(self.project["events"][5])
        new_event["id"] = "evt-new-001"
        new_event["click_ms"] += 500.0
        self.engine.upsert_event(new_event)
        report, scope = recompute_incremental(
            self.engine,
            self.baseline,
            changed_event_ids=[new_event["id"]],
            reason="unit_test_new_event",
        )
        self.assertIn(new_event["id"], report["event_order"])
        full = RhythmEngine(self.engine.project).run_full()
        self.assertEqual(self._core(report), self._core(full))


class SnapshotTest(unittest.TestCase):
    def test_snapshot_roundtrip_preserves_untouched_results(self):
        project = generate_project()
        engine = RhythmEngine(project)
        baseline = engine.run_full()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snapshot.json")
            save_snapshot(path, baseline, project)
            loaded = load_snapshot(path)
            verify_snapshot(loaded)
            self.assertEqual(
                loaded["integrity"]["result_sha256"],
                self._baseline_hashes(baseline),
            )

            target = project["events"][2]
            modified = dict(target)
            modified["click_ms"] += 21.0
            engine.upsert_event(modified)
            report, scope = recompute_incremental(
                engine, baseline, changed_event_ids=[target["id"]], reason="resume_test"
            )
            untouched = {
                event_id: entry
                for event_id, entry in loaded["results"].items()
                if event_id != target["id"]
            }
            current = {item["event_id"]: item for item in report["results"]}
            for event_id, entry in untouched.items():
                self.assertEqual(entry, current[event_id])

    @staticmethod
    def _baseline_hashes(report):
        from rhythm_kernel.model import sha256_hex

        return {
            result["event_id"]: sha256_hex(result)
            for result in report["results"]
        }


class GeneratorTest(unittest.TestCase):
    def test_generator_is_deterministic(self):
        self.assertEqual(generate_project(20261128), generate_project(20261128))

    def test_generated_project_passes_reference_judge(self):
        project = generate_project()
        report = RhythmEngine(project).run_full()
        self.assertTrue(compare_with_kernel(report, project)["ok"])
        facts = reference_judge(project)
        self.assertEqual(len(facts), len(project["events"]))


if __name__ == "__main__":
    unittest.main()
