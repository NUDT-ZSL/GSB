from __future__ import annotations

import unittest

from hdr_color.stats import build_windows


def fake_frame(index: int, value: float) -> dict[str, object]:
    return {
        "frame_index": index,
        "valid": True,
        "status": "ok",
        "luminance_peak_nits": value,
        "_pixel_count": 2,
        "_over_count": 0,
        "_luminance_sum": value * 2,
    }


class WindowStatisticsTests(unittest.TestCase):
    def test_tail_include_and_drop(self) -> None:
        frames = [fake_frame(index, float(index + 1)) for index in range(7)]
        included = build_windows(frames, 7, 3, "include")
        dropped = build_windows(frames, 7, 3, "drop")
        self.assertEqual(
            [(item["start_frame"], item["end_frame_exclusive"]) for item in included],
            [(0, 3), (3, 6), (6, 7)],
        )
        self.assertTrue(included[-1]["is_partial_tail"])
        self.assertEqual(
            [(item["start_frame"], item["end_frame_exclusive"]) for item in dropped],
            [(0, 3), (3, 6)],
        )
        self.assertFalse(any(item["is_partial_tail"] for item in dropped))

    def test_invalid_frames_are_excluded_from_values(self) -> None:
        frames = [
            fake_frame(0, 10.0),
            {"frame_index": 1, "valid": False, "status": "error"},
            fake_frame(2, 20.0),
        ]
        windows = build_windows(frames, 3, 3, "include")
        self.assertEqual(windows[0]["valid_frame_count"], 2)
        self.assertEqual(windows[0]["invalid_frame_count"], 1)
        self.assertEqual(windows[0]["luminance_mean_nits"], 15.0)
        self.assertEqual(windows[0]["luminance_peak_nits"], 20.0)


if __name__ == "__main__":
    unittest.main()
