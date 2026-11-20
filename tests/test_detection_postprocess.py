import tempfile
import unittest
from pathlib import Path

from aed_scale.catalog import ACTIVE_EVENT_TYPES
from aed_scale.codes import BusinessCode
from aed_scale.config import DetectionConfig, FeatureConfig
from aed_scale.detection import detect_candidates, validate_candidates
from aed_scale.errors import AedError
from aed_scale.features import extract_features
from aed_scale.io import load_manifest, load_truth_frames, load_wav_pcm16_mono
from aed_scale.paths import path_profiles
from aed_scale.postprocess import (
    postprocess_candidates,
    validate_event_intervals,
)
from aed_scale.synthesis import generate_dataset
from aed_scale.types import Candidate, TimelineSegment


class DetectionPostprocessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tempdir.name)
        generate_dataset(
            cls.root,
            seed=99,
            num_dialogs=1,
            duration_ms=2_200,
            speaker_count=3,
        )
        row = load_manifest(cls.root / "manifest.jsonl")[0]
        cls.dialog_id = row["dialog_id"]
        cls.pcm, _ = load_wav_pcm16_mono(cls.root / row["wav_path"])
        cls.truth = load_truth_frames(cls.root / row["truth_path"])
        cls.config = FeatureConfig()
        cls.detection_config = DetectionConfig()
        cls.features = extract_features(
            cls.pcm,
            cls.config,
            dialog_id=cls.dialog_id,
            truth_frame_count=len(cls.truth),
        )

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def test_detection_is_deterministic_and_controlled(self):
        profile = path_profiles(self.detection_config)["fine"]
        first = detect_candidates(
            self.features,
            profile,
            detection_config=self.detection_config,
        )
        second = detect_candidates(
            self.features,
            profile,
            detection_config=self.detection_config,
        )
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertGreater(len(first.candidates), 0)
        self.assertTrue(all(candidate.event_type in ACTIVE_EVENT_TYPES for candidate in first.candidates))
        sorted_keys = [
            (
                -round(candidate.confidence, 12),
                ACTIVE_EVENT_TYPES.index(candidate.event_type),
                candidate.start_frame,
                candidate.end_frame,
            )
            for candidate in first.candidates
        ]
        self.assertEqual(sorted_keys, sorted(sorted_keys))

    def test_candidate_type_and_range_errors(self):
        with self.assertRaises(AedError) as type_context:
            validate_candidates(
                [Candidate(0, 3, "siren", 0.9)],
                len(self.truth),
                dialog_id=self.dialog_id,
            )
        self.assertEqual(type_context.exception.code, BusinessCode.E_EVENT_TYPE.value)
        self.assertEqual(type_context.exception.details["event_index"], 0)
        self.assertEqual(type_context.exception.dialog_id, self.dialog_id)

        for candidate in (
            Candidate(-1, 2, "cough", 0.9),
            Candidate(0, len(self.truth), "cough", 0.9),
            Candidate(5, 4, "cough", 0.9),
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(AedError) as range_context:
                    validate_candidates(
                        [candidate],
                        len(self.truth),
                        dialog_id=self.dialog_id,
                    )
                self.assertEqual(
                    range_context.exception.code,
                    BusinessCode.E_EVENT_RANGE.value,
                )
                self.assertEqual(range_context.exception.dialog_id, self.dialog_id)

    def test_postprocess_covers_all_frames_and_merges_gap(self):
        profile = path_profiles(self.detection_config)["fine"]
        candidates = [
            Candidate(10, 19, "cough", 0.8),
            Candidate(22, 31, "cough", 0.7),
        ]
        segments = postprocess_candidates(
            candidates,
            frame_count=100,
            duration_samples=16_000,
            config=self.config,
            profile=profile,
            min_event_ms=20,
            min_gap_ms=30,
            dialog_id="dialog_merge",
        )
        self.assertEqual(segments[0].start_frame, 0)
        self.assertEqual(segments[-1].end_frame, 99)
        self.assertEqual(
            [segment.start_frame for segment in segments[1:]],
            [segment.end_frame + 1 for segment in segments[:-1]],
        )
        events = [segment for segment in segments if segment.is_event]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].start_frame, events[0].end_frame), (10, 31))
        self.assertAlmostEqual(events[0].start_ms, 100.0)
        self.assertAlmostEqual(events[0].end_ms, 320.0)

    def test_manual_overlap_and_out_of_range_errors(self):
        overlapping = [
            TimelineSegment(0, 20, 0.0, 210.0, "cough", 0.8, True, "a"),
            TimelineSegment(15, 30, 150.0, 310.0, "knock", 0.7, True, "b"),
        ]
        with self.assertRaises(AedError) as overlap_context:
            validate_event_intervals(
                overlapping,
                duration_ms=1_000,
                dialog_id="dialog_overlap",
            )
        self.assertEqual(overlap_context.exception.code, BusinessCode.E_OVERLAP.value)
        self.assertEqual(overlap_context.exception.dialog_id, "dialog_overlap")

        outside = [
            TimelineSegment(0, 20, 0.0, 950.0, "cough", 0.8, True, "a"),
            TimelineSegment(21, 30, 920.0, 1_100.0, "knock", 0.7, True, "b"),
        ]
        with self.assertRaises(AedError) as range_context:
            validate_event_intervals(
                outside,
                duration_ms=1_000,
                dialog_id="dialog_outside",
            )
        self.assertEqual(range_context.exception.code, BusinessCode.E_OUT_OF_RANGE.value)
        self.assertEqual(range_context.exception.dialog_id, "dialog_outside")


if __name__ == "__main__":
    unittest.main()
