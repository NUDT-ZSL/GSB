from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from underwater_pipeline.config import DEFAULT_CONFIG
from underwater_pipeline.errors import PipelineError
from underwater_pipeline.generator import generate_sample
from underwater_pipeline.pipeline import analyze_frames
from underwater_pipeline.report import build_report, write_result_bundle


class PipelineAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sample = generate_sample(20240517, n_frames=480)
        cls.result = analyze_frames(
            cls.sample.frames,
            cls.sample.timestamps,
            config=DEFAULT_CONFIG,
            seed=20240517,
            truth=cls.sample.truth,
        )

    def test_fixed_seed_is_item_for_item_reproducible(self) -> None:
        replay = generate_sample(20240517, n_frames=480)
        replayed = analyze_frames(
            replay.frames,
            replay.timestamps,
            seed=20240517,
            truth=replay.truth,
        )
        self.assertTrue(np.array_equal(self.sample.frames, replay.frames))
        self.assertEqual(
            self.result["boundaries"], replayed["boundaries"]
        )
        self.assertEqual(
            self.result["visibility"], replayed["visibility"]
        )
        self.assertEqual(
            self.result["teaching_segments"],
            replayed["teaching_segments"],
        )
        self.assertEqual(self.result["events"], replayed["events"])
        self.assertEqual(
            self.result["content_hash"], replayed["content_hash"]
        )

    def test_true_cuts_are_recovered_exactly(self) -> None:
        comparison = self.result["truth_comparison"]
        self.assertTrue(comparison["cut_exact_match"])
        self.assertEqual(comparison["missing_cut_frames"], [])
        self.assertEqual(comparison["extra_cut_frames"], [])
        self.assertEqual(
            self.result["boundaries"]["stable_transition_frames"],
            [90, 178, 272, 378],
        )

    def test_single_frame_flash_is_removed(self) -> None:
        removals = self.result["boundaries"]["removals"]
        flash = self.sample.truth["false_cut_injection_frames"][0]
        reasons = {
            item["frame_index"]: item["reason"]
            for item in removals
            if item["reason"] == "single_frame_segment_removed"
        }
        self.assertIn(flash, reasons)
        self.assertIn(flash + 1, reasons)

    def test_turbidity_is_not_turned_into_a_shot_cut(self) -> None:
        cuts = set(self.result["boundaries"]["stable_transition_frames"])
        interval = self.sample.truth["true_haze_intervals"][0]
        self.assertNotIn(interval["start_frame"], cuts)
        self.assertNotIn(interval["end_frame_exclusive"], cuts)
        self.assertTrue(self.result["detector_collisions"])
        self.assertTrue(
            all(
                item["decision"] == "turbidity_change"
                for item in self.result["detector_collisions"]
            )
        )

    def test_visibility_and_ownership_are_consistent(self) -> None:
        report = build_report(self.result)
        self.assertTrue(report["consistency"]["all_passed"])
        shot_ids = report["shot_ownership"]["shot_ids"]
        flat = report["shot_ownership"]["flat_segment_shot_ids"]
        self.assertEqual(shot_ids, flat)
        self.assertEqual(len(flat), len(set(flat)))
        for shot in self.result["visibility"]["shots"]:
            self.assertGreaterEqual(shot["score"], 0.0)
            self.assertLessEqual(shot["score"], 100.0)
            self.assertIn(
                shot["turbidity_level_name"],
                {
                    "clear",
                    "light_turbidity",
                    "moderate_turbidity",
                    "heavy_turbidity",
                },
            )

    def test_second_seed_has_different_turbidity_distribution(self) -> None:
        second = generate_sample(20240518, n_frames=480)
        result = analyze_frames(
            second.frames,
            second.timestamps,
            seed=20240518,
            truth=second.truth,
        )
        self.assertTrue(result["truth_comparison"]["cut_exact_match"])
        self.assertTrue(result["consistency"]["all_passed"])
        self.assertEqual(len(result["turbidity_intervals"]), 2)
        self.assertEqual(
            len(self.result["turbidity_intervals"]), 1
        )
        self.assertNotEqual(
            result["content_hash"], self.result["content_hash"]
        )

    def test_empty_frames_has_business_error(self) -> None:
        with self.assertRaises(PipelineError) as context:
            analyze_frames(
                np.empty((0, 24, 24, 3), dtype=np.uint8),
                np.asarray([], dtype=np.float64),
            )
        self.assertEqual(context.exception.code, "E_EMPTY_FRAMES")
        self.assertEqual(
            context.exception.to_dict()["error"]["code"],
            "E_EMPTY_FRAMES",
        )

    def test_non_monotonic_timestamps_have_business_error(self) -> None:
        frames = np.zeros((4, 24, 24, 3), dtype=np.uint8)
        with self.assertRaises(PipelineError) as context:
            analyze_frames(frames, np.asarray([0.0, 0.1, 0.1, 0.3]))
        self.assertEqual(
            context.exception.code,
            "E_TIMESTAMPS_NOT_MONOTONIC",
        )

    def test_bundle_writer_produces_reviewable_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifacts = write_result_bundle(directory, self.result)
            for key in (
                "analysis",
                "boundaries",
                "visibility",
                "segments",
                "events",
                "report_json",
                "report_md",
            ):
                self.assertIn(key, artifacts)
                self.assertTrue(Path(artifacts[key]).exists())


if __name__ == "__main__":
    unittest.main()
