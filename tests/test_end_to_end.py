from __future__ import annotations

import tempfile
import unittest
import copy
import json
from pathlib import Path

from audio_qa.acceptance import build_acceptance
from audio_qa.analysis import analyze_manifest
from audio_qa.dual import compare_runs
from audio_qa.generator import generate_dataset
from audio_qa.verification import verify_segment


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls._tmp.name) / "data"
        generate_dataset(cls.data_dir, seed=20261128)
        cls.manifest = cls.data_dir / "manifest.json"
        cls.primary = analyze_manifest(cls.manifest, 512, 128)
        cls.comparison = analyze_manifest(cls.manifest, 1024, 256)
        cls.dual = compare_runs(cls.primary, cls.comparison)
        cls.acceptance = build_acceptance(cls.primary, cls.comparison, cls.dual)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_all_truth_segments_verified(self):
        summary = self.primary["summary"]
        self.assertEqual(summary["truth_available_segments"], 20)
        self.assertEqual(summary["verified_segments"], 20)
        self.assertEqual(summary["inconsistent_segments"], 0)
        self.assertEqual(summary["unverifiable_segments"], 1)

    def test_no_false_positives_or_negatives(self):
        for damage_type, stats in self.acceptance["type_stats"].items():
            self.assertEqual(stats["false_negative_count"], 0, damage_type)
            self.assertEqual(stats["false_positive_count"], 0, damage_type)
            self.assertEqual(stats["grade_matches"], stats["grade_comparisons"], damage_type)

    def test_adaptive_noise_and_dual_frame_acceptance(self):
        self.assertTrue(self.acceptance["pass"])
        self.assertLessEqual(self.acceptance["noise_floor"]["max_mae_db"], 4.0)
        self.assertGreaterEqual(
            self.acceptance["noise_floor"]["min_within_tolerance_fraction"], 0.80
        )
        self.assertGreater(
            self.acceptance["noise_floor"]["median_improvement_over_frame_rms_db"], 10.0
        )
        self.assertTrue(self.dual["stable"])
        self.assertEqual(self.dual["stable_segment_count"], self.dual["segment_count"])

    def test_truth_missing_segment_reports_detections_but_not_checked(self):
        segments = {
            segment["segment_id"]: segment
            for recording in self.primary["recordings"]
            for segment in recording["segments"]
        }
        missing = segments["archive_F_mixed/seg_02"]
        self.assertEqual(missing["verification"]["status"], "unverifiable")
        self.assertGreater(sum(len(v) for v in missing["damage_intervals"].values()), 0)

    def test_incomplete_frames_are_tail_suffix(self):
        for recording in self.primary["recordings"]:
            for segment in recording["segments"]:
                frames = segment["frames"]
                incomplete = [index for index, frame in enumerate(frames) if not frame["complete"]]
                if incomplete:
                    self.assertEqual(incomplete, list(range(incomplete[0], len(frames))))
                    self.assertTrue(
                        all(frame["end_sample"] == segment["end_sample"] for frame in frames[incomplete[0] :])
                    )

    def test_injected_truth_mismatch_reports_first_evidence(self):
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        recording = manifest["recordings"][0]
        segment_spec = copy.deepcopy(recording["segments"][0])
        detected = copy.deepcopy(self.primary["recordings"][0]["segments"][0])
        segment_spec["ground_truth"]["damages"]["clipping"][0]["start_sample"] += 5000
        segment_spec["ground_truth"]["damages"]["clipping"][0]["grade"] = "heavy"
        segment_spec["ground_truth"]["segment_grades"]["clipping"] = "heavy"
        verification = verify_segment(
            segment_spec,
            detected,
            detected["frames"],
            detected["sample_rate"],
        )
        self.assertEqual(verification["status"], "inconsistent")
        self.assertTrue(verification["first_issue_kind"])
        self.assertTrue(verification["frame_evidence"])

    def test_dual_comparison_surfaces_inconsistency(self):
        comparison = copy.deepcopy(self.comparison)
        segment = comparison["recordings"][0]["segments"][0]
        segment["damage_intervals"]["clipping"][0]["start_sample"] += 1000
        segment["segment_grades"]["clipping"] = "heavy"
        result = compare_runs(self.primary, comparison)
        self.assertFalse(result["stable"])
        self.assertIn(segment["segment_id"], result["review_segment_ids"])
        unstable = [item for item in result["segments"] if item["segment_id"] == segment["segment_id"]][0]
        self.assertTrue(unstable["differences"])


if __name__ == "__main__":
    unittest.main()
