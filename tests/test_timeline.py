import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from mocap_audio.timeline import _runs, stitch


class FakeSeg:
    def __init__(self, device_id, segment_id, start_ms, channels, gain):
        self.device_id = device_id
        self.segment_id = segment_id
        self.start_ms = start_ms
        self.duration_ms = 0
        self.channels = channels
        self.gain = gain


def contribution(start, end, value, seg, channels):
    frames = end - start + 1
    samples = np.full((frames, len(channels)), value, dtype=np.float64)
    return {"ref_start": start, "ref_end": end, "samples": samples,
            "seg": seg, "device_rate": 48000}


class TimelineTest(unittest.TestCase):
    def test_gap_fill_and_slice_marks(self):
        channels = ["A", "B"]
        seg1 = FakeSeg("D1", "S1", 0, channels, [1.0, 1.0])
        seg2 = FakeSeg("D2", "S2", 0, channels, [1.0, 1.0])
        contributions = [
            contribution(0, 95, 0.5, seg1, channels),
            contribution(120, 199, 0.25, seg2, channels),
        ]
        result = stitch(contributions, 48000, 200, channels,
                        timeslice_ms=2)  # 96 帧/片
        self.assertTrue(np.all(result["timeline"][0:96] == 0.5))
        self.assertTrue(np.all(result["timeline"][96:120] == 0.0))
        self.assertTrue(np.all(result["gap_mask"][96:120]))
        self.assertFalse(np.any(result["gap_mask"][0:96]))
        gap = result["gap_ranges"][0]
        self.assertEqual((gap["start_frame"], gap["end_frame"]), (96, 119))
        gap_slices = [s for s in result["slices"] if s["gap_frames"]]
        self.assertTrue(gap_slices)
        self.assertEqual(gap_slices[0]["gap_frames"], 24)
        owners = {o["segment_id"] for o in result["slices"][0]["ownership"]}
        self.assertIn("S1", owners)

    def test_first_declaration_wins_on_overlap(self):
        channels = ["A"]
        seg1 = FakeSeg("D1", "S1", 0, channels, [1.0])
        seg2 = FakeSeg("D2", "S2", 0, channels, [1.0])
        contributions = [
            contribution(10, 30, 0.7, seg1, channels),
            contribution(20, 40, 0.1, seg2, channels),
        ]
        result = stitch(contributions, 48000, 50, channels, timeslice_ms=10)
        self.assertEqual(result["timeline"][25, 0], 0.7)
        self.assertEqual(result["timeline"][35, 0], 0.1)

    def test_runs_helper(self):
        mask = np.array([0, 1, 1, 0, 0, 1], dtype=bool)
        self.assertEqual(_runs(mask), [(1, 2), (5, 5)])


if __name__ == "__main__":
    unittest.main()

