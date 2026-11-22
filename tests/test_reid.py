from __future__ import annotations

import unittest

import numpy as np

from meeting_post.config import default_config
from meeting_post.errors import MeetingError
from meeting_post.reid import assign_identities

from tests.helpers import make_result


class ReidTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result, cls.store, cls.temp = make_result()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_identity_labels_and_confidence_bounds(self) -> None:
        declared = {spec.speaker_id for spec in self.result.session.speaker_specs}
        for identity in self.result.identities:
            self.assertIn(identity.speaker_id, declared)
            self.assertGreaterEqual(identity.confidence_low, 0.0)
            self.assertLessEqual(identity.confidence_high, 1.0)
            self.assertLessEqual(identity.confidence_low, identity.confidence_high)
            self.assertTrue(identity.truth_speaker_ids)
            self.assertTrue(identity.matches_truth)

    def test_unknown_profile_speaker_is_rejected(self) -> None:
        profiles = dict(self.result.profiles)
        profiles[99] = profiles[1]
        with self.assertRaises(MeetingError) as context:
            assign_identities(
                default_config("v3"),
                self.result.features,
                profiles,
                self.result.separation.segments,
                session_truth=self.result.session.truth,
                session_speaker_ids={1, 2, 3},
            )
        self.assertEqual(context.exception.code, "E_SPEAKER_UNKNOWN")
        self.assertEqual(context.exception.details["speaker_id"], 99)

    def test_tie_breaks_by_smallest_speaker_id(self) -> None:
        config = default_config("v3")
        profiles = {
            speaker_id: {
                "mean": np.asarray([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
                "std": np.zeros(8),
                "frames": 1,
            }
            for speaker_id in (1, 2, 3)
        }
        assignments = assign_identities(
            config,
            self.result.features,
            profiles,
            self.result.separation.segments[:1],
            session_truth=self.result.session.truth,
            session_speaker_ids={1, 2, 3},
        )
        self.assertEqual(assignments[0].speaker_id, 1)


if __name__ == "__main__":
    unittest.main()
