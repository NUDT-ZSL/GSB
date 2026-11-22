from __future__ import annotations

import unittest

from meeting_post.config import default_config
from meeting_post.errors import MeetingError
from meeting_post.timebase import (
    SegmentSpan,
    build_span,
    ms_to_sample,
    sample_to_ms,
    verify_delivery_interval,
    verify_span_timebase,
)


class TimebaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = default_config("v3")

    def test_delivery_roundtrip(self) -> None:
        for sample in (0, 1, 319, 320, 6400, 21760, 64000):
            delivery = sample_to_ms(sample, 16000)
            self.assertEqual(ms_to_sample(delivery, 16000), sample)

    def test_segment_roundtrip(self) -> None:
        span = build_span(
            segment_id="SEG-T",
            speaker_id=1,
            start_frame=20,
            end_frame=68,
            sample_rate=16000,
            frame_samples=320,
            hop_samples=320,
        )
        row = verify_span_timebase(
            span,
            sample_rate=16000,
            frame_samples=320,
            hop_samples=320,
        )
        self.assertEqual(row["start_ms"], 400.0)
        self.assertEqual(row["end_ms"], 1360.0)
        self.assertEqual(row["inverse_start_frame"], 20)
        self.assertEqual(row["inverse_end_frame"], 68)

    def test_zero_frame_span_is_rejected(self) -> None:
        span = SegmentSpan("SEG-Z", 1, 20, 20, 6400, 6400)
        with self.assertRaises(MeetingError) as context:
            verify_span_timebase(
                span,
                sample_rate=16000,
                frame_samples=320,
                hop_samples=320,
            )
        error = context.exception
        self.assertEqual(error.code, "E_TIME_BASE_MISMATCH")
        self.assertEqual(error.details["segment_id"], "SEG-Z")
        self.assertEqual(error.details["frame_interval"], [20, 20])

    def test_wrong_sample_is_rejected_with_frame_interval(self) -> None:
        span = SegmentSpan("SEG-W", 1, 20, 68, 6401, 21760)
        with self.assertRaises(MeetingError) as context:
            verify_span_timebase(
                span,
                sample_rate=16000,
                frame_samples=320,
                hop_samples=320,
            )
        self.assertEqual(context.exception.code, "E_TIME_BASE_MISMATCH")
        self.assertEqual(context.exception.details["segment_id"], "SEG-W")
        self.assertEqual(context.exception.details["frame_interval"], [20, 68])

    def test_wrong_delivered_milliseconds_are_rejected(self) -> None:
        with self.assertRaises(MeetingError) as context:
            verify_delivery_interval(
                segment_id="SEG-MS",
                start_ms=400.5,
                end_ms=1360.0,
                start_frame=20,
                end_frame=68,
                sample_rate=16000,
                frame_samples=320,
                hop_samples=320,
            )
        error = context.exception
        self.assertEqual(error.code, "E_TIME_BASE_MISMATCH")
        self.assertEqual(error.details["segment_id"], "SEG-MS")
        self.assertEqual(error.details["frame_interval"], [20, 68])


if __name__ == "__main__":
    unittest.main()
