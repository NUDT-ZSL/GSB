from __future__ import annotations

import unittest

from meeting_post.config import default_config
from meeting_post.features import compute_features
from meeting_post.synthesis import synthesize_meeting


class SynthesisFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = default_config("v3")
        cls.first = synthesize_meeting(cls.config, seed=20261123)
        cls.second = synthesize_meeting(cls.config, seed=20261123)

    def test_pcm_and_truth_are_byte_identical(self) -> None:
        self.assertEqual(self.first.pcm_bytes, self.second.pcm_bytes)
        self.assertEqual(self.first.truth_bytes, self.second.truth_bytes)
        self.assertEqual(self.first.pcm_sha256, self.second.pcm_sha256)

    def test_frame_count_matches_truth(self) -> None:
        frames = {row["frame_index"] for row in self.first.truth}
        self.assertEqual(len(frames), self.first.frame_count)
        self.assertEqual(self.first.frame_count, 200)
        self.assertEqual(len(self.first.truth), 600)

    def test_truth_fields_and_sample_boundaries(self) -> None:
        for row in self.first.truth:
            self.assertEqual(
                set(row),
                {
                    "frame_index",
                    "speaker_id",
                    "is_speech",
                    "sample_start",
                    "sample_end",
                },
            )
            self.assertEqual(row["sample_start"], row["frame_index"] * 320)
            self.assertEqual(row["sample_end"] - row["sample_start"], 320)
            self.assertLessEqual(row["sample_end"], self.first.total_samples)

    def test_features_are_byte_identical_and_aligned(self) -> None:
        first = compute_features(self.config, self.first)
        second = compute_features(self.config, self.second)
        self.assertEqual(first.feature_bytes, second.feature_bytes)
        self.assertEqual(first.feature_sha256, second.feature_sha256)
        self.assertEqual(len(first.frames), self.first.frame_count)
        truth_by_frame = {
            row["frame_index"]: row
            for row in self.first.truth
            if row["speaker_id"] == 1
        }
        for frame in first.frames:
            truth = truth_by_frame[frame["frame_index"]]
            self.assertEqual(frame["frame_index"], truth["frame_index"])
            self.assertEqual(frame["sample_start"], truth["sample_start"])
            self.assertEqual(frame["sample_end"], truth["sample_end"])
            self.assertGreaterEqual(frame["energy"], 0.0)
            self.assertGreaterEqual(frame["zcr"], 0.0)
            self.assertLessEqual(frame["zcr"], 1.0)

    def test_different_seed_changes_pcm(self) -> None:
        other = synthesize_meeting(self.config, seed=20261124)
        self.assertNotEqual(other.pcm_sha256, self.first.pcm_sha256)


if __name__ == "__main__":
    unittest.main()
