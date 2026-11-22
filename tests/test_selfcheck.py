from __future__ import annotations

import unittest

from meeting_post.errors import MeetingError
from meeting_post.selfcheck import run_self_check

from tests.helpers import make_result


class SelfCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result, cls.store, cls.temp = make_result()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_report_contains_required_checks(self) -> None:
        report = run_self_check(self.result)
        self.assertTrue(report["ok"])
        self.assertEqual(
            set(report["checks"]),
            {
                "segmentation_mask_coverage",
                "separated_segment_mask_coverage",
                "identity_segment_one_to_one",
                "timebase_roundtrip",
                "segments_within_audio",
                "identity_matches_truth",
                "identity_labels_declared",
            },
        )

    def test_broken_identity_mapping_raises_inconsistent(self) -> None:
        result, _, temp = make_result()
        try:
            result.identities.pop()
            with self.assertRaises(MeetingError) as context:
                run_self_check(result, raise_on_error=True)
            self.assertEqual(context.exception.code, "E_INCONSISTENT")
            self.assertTrue(context.exception.details["issues"])
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
