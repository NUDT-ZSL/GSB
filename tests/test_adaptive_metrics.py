import json
import unittest

from aed_scale.adaptive import ResourceConstraints, ScaleObservation, decide_path
from aed_scale.codes import BusinessCode
from aed_scale.errors import AedError
from aed_scale.metrics import (
    compute_dialog_metrics,
    flatten_frame_decisions,
    metrics_from_recorded_details,
)
from aed_scale.types import TimelineSegment


class AdaptiveMetricsTests(unittest.TestCase):
    def setUp(self):
        self.constraints = ResourceConstraints(
            max_resident_mb=0.5,
            max_candidate_comparisons=1_000,
            max_fine_frames=1_000,
            min_recall_gain=0.02,
        )
        self.small = ScaleObservation(
            dialog_count=2,
            total_frames=440,
            avg_candidates_per_frame=0.06,
            speaker_count=3,
            feature_dimension=10,
            dialog_ids=("dialog_000", "dialog_001"),
            recall_gain_estimate=0.05,
        )
        self.large = ScaleObservation(
            dialog_count=8,
            total_frames=2_400,
            avg_candidates_per_frame=0.06,
            speaker_count=4,
            feature_dimension=10,
            dialog_ids=tuple(f"dialog_{index:03d}" for index in range(8)),
            recall_gain_estimate=0.05,
        )

    def test_scale_switch_is_deterministic_and_covered(self):
        first = decide_path(self.small, self.constraints)
        second = decide_path(self.small, self.constraints)
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")),
            json.dumps(second, sort_keys=True, separators=(",", ":")),
        )
        self.assertEqual(first["selected_path"], "fine")
        self.assertFalse(first["degraded"])
        self.assertTrue(first["planned_coverage"]["all_dialogs"])
        self.assertTrue(first["planned_coverage"]["all_frames"])

        large = decide_path(self.large, self.constraints)
        self.assertEqual(large["selected_path"], "approximate")
        self.assertTrue(large["degraded"])
        self.assertEqual(set(large["path_by_dialog"].values()), {"approximate"})
        self.assertEqual(len(large["path_by_dialog"]), self.large.dialog_count)
        reasons = " ".join(large["decision_reasons"])
        self.assertIn("resident-memory", reasons)
        self.assertIn("comparisons", reasons)
        self.assertIn("frames", reasons)
        self.assertEqual(len(large["decision_sha256"]), 64)

    def test_invalid_observation(self):
        bad = ScaleObservation(
            dialog_count=0,
            total_frames=0,
            avg_candidates_per_frame=-1,
            speaker_count=0,
            feature_dimension=0,
        )
        with self.assertRaises(AedError) as context:
            decide_path(bad, self.constraints)
        self.assertEqual(context.exception.code, BusinessCode.E_CONFIG_INVALID.value)

    def test_metrics_replay_from_frame_and_event_details(self):
        truth_frames = []
        for index in range(10):
            active = 2 <= index <= 5
            truth_frames.append(
                {
                    "frame_index": index,
                    "event_id": "evt_1" if active else None,
                    "event_type": "cough" if active else "background",
                    "speaker_id": None,
                    "is_speech": False,
                }
            )
        segments = [
            TimelineSegment(0, 2, 0.0, 30.0, "background", 0.0, False, "s0"),
            TimelineSegment(3, 6, 30.0, 70.0, "cough", 0.8, True, "s1"),
            TimelineSegment(7, 9, 70.0, 100.0, "background", 0.0, False, "s2"),
        ]
        decisions = flatten_frame_decisions("dialog_metric", truth_frames, segments)
        metrics = compute_dialog_metrics(
            dialog_id="dialog_metric",
            truth_frames=truth_frames,
            segments=segments,
            frame_decisions=decisions,
            hop_samples=10,
            sample_rate=16_000,
            duration_samples=160,
            overlap_threshold=0.5,
        )
        self.assertAlmostEqual(metrics["frame"]["frame_accuracy"], 0.8)
        self.assertAlmostEqual(metrics["frame"]["event_frame_recall"], 0.75)
        self.assertAlmostEqual(metrics["frame"]["event_frame_precision"], 0.75)
        self.assertEqual(metrics["event"]["truth_count"], 1)
        self.assertEqual(metrics["event"]["prediction_count"], 1)
        self.assertEqual(metrics["event"]["matched_count"], 1)
        self.assertAlmostEqual(metrics["event"]["event_recall"], 1.0)
        recomputed = metrics_from_recorded_details(
            decisions,
            metrics["event"]["details"],
        )
        self.assertAlmostEqual(
            recomputed["frame"]["frame_accuracy"],
            metrics["frame"]["frame_accuracy"],
        )
        self.assertAlmostEqual(
            recomputed["event"]["event_recall"],
            metrics["event"]["event_recall"],
        )


if __name__ == "__main__":
    unittest.main()
