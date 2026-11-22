from __future__ import annotations

import unittest
import tempfile
from dataclasses import replace
from pathlib import Path

from meeting_post.config import default_config, parse_config, render_config
from meeting_post.demo import stress_config
from meeting_post.features import compute_features
from meeting_post.pipeline import process_config
from meeting_post.selfcheck import run_self_check
from meeting_post.snapshots import SnapshotStore
from meeting_post.synthesis import synthesize_meeting
from meeting_post.timebase import verify_span_timebase

from tests.helpers import make_result


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result, cls.store, cls.temp = make_result()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_segments_masks_and_identities(self) -> None:
        self.assertEqual(len(self.result.separation.candidates), 3)
        self.assertEqual(len(self.result.separation.segments), 3)
        self.assertEqual(len(self.result.identities), 3)
        for segment, identity in zip(
            self.result.separation.segments, self.result.identities
        ):
            self.assertEqual(segment.segment_id, identity.segment_id)
            self.assertTrue(identity.matches_truth)
            self.assertGreaterEqual(identity.confidence_low, 0.0)
            self.assertLessEqual(identity.confidence_high, 1.0)
            self.assertEqual(identity.speaker_id, segment.speaker_id)

    def test_segments_do_not_overlap_and_stay_in_audio(self) -> None:
        previous_end = 0
        for segment in self.result.separation.segments:
            self.assertGreaterEqual(segment.start_sample, previous_end)
            self.assertLess(segment.start_sample, segment.end_sample)
            self.assertLessEqual(segment.end_sample, self.result.config.total_samples)
            previous_end = segment.end_sample

    def test_timebase_verifies(self) -> None:
        for segment in self.result.separation.segments:
            verify_span_timebase(
                segment,
                sample_rate=self.result.config.sample_rate,
                frame_samples=self.result.config.frame_samples,
                hop_samples=self.result.config.hop_samples,
            )

    def test_self_check_passes(self) -> None:
        self.assertTrue(self.result.self_check["ok"])
        self.assertTrue(run_self_check(self.result)["ok"])

    def test_repeat_processing_is_byte_stable(self) -> None:
        repeated = process_config(default_config("v3"), self.store)
        self.assertEqual(
            repeated.separation.mask_bytes,
            self.result.separation.mask_bytes,
        )
        self.assertEqual(
            repeated.separation.segment_bytes,
            self.result.separation.segment_bytes,
        )
        self.assertEqual(repeated.result_digest, self.result.result_digest)

    def test_all_three_config_versions_produce_same_result(self) -> None:
        canonical = default_config("v3")
        digests = set()
        for version in ("v1", "v2", "v3"):
            config = parse_config(render_config(canonical, version))
            result = process_config(config, self.store)
            digests.add(result.result_digest)
            self.assertTrue(result.self_check["ok"])
        self.assertEqual(len(digests), 1)

    def test_v2_overlapping_hop_keeps_truth_and_features_aligned(self) -> None:
        config = replace(
            default_config("v3"),
            hop_ms=10.0,
            speaker_gains=((1, 1.0), (2, 1.0), (3, 1.0)),
        )
        parsed = parse_config(render_config(config, "v2"))
        session = synthesize_meeting(parsed, seed=20261123)
        features = compute_features(parsed, session)
        self.assertEqual(parsed.frame_count, 399)
        self.assertEqual(len(features.frames), len({row["frame_index"] for row in session.truth}))
        for frame in features.frames:
            self.assertEqual(frame["sample_end"] - frame["sample_start"], 320)
        with tempfile.TemporaryDirectory() as temp:
            store = SnapshotStore(Path(temp))
            store.update_from_config(parsed)
            result = process_config(parsed, store)
            self.assertTrue(result.self_check["ok"])
            self.assertTrue(all(item.matches_truth for item in result.identities))

    def test_float32_weighted_stress_config_runs_and_self_checks(self) -> None:
        config = stress_config()
        with tempfile.TemporaryDirectory() as temp:
            store = SnapshotStore(Path(temp))
            store.update_from_config(config)
            result = process_config(config, store)
            self.assertEqual(result.features.embeddings.dtype.name, "float32")
            self.assertTrue(result.self_check["ok"])
            self.assertTrue(all(item.matches_truth for item in result.identities))


if __name__ == "__main__":
    unittest.main()
