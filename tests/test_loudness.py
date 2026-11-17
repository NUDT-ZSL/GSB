import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from mocap_audio.loudness import measure, window_boundaries


class FakeSeg:
    def __init__(self, device_id, segment_id):
        self.device_id = device_id
        self.segment_id = segment_id


def contribution(start, end, seg):
    return {"ref_start": start, "ref_end": end, "samples": None,
            "seg": seg, "device_rate": 48000}


class LoudnessTest(unittest.TestCase):
    def test_window_alignment_to_integer_ms(self):
        bounds = window_boundaries(48000, 100, 14400, "drop")
        self.assertEqual(bounds[0], (0, 4800))
        self.assertEqual(bounds[-1], (9600, 14400))
        self.assertEqual(len(bounds), 3)
        include = window_boundaries(48000, 100, 15000, "include")
        self.assertEqual(include[-1], (14400, 15000))

    def test_dual_gate_and_attribution(self):
        frames = 9600
        timeline = np.zeros((frames, 1))
        timeline[0:4800, 0] = 0.5
        timeline[4800:9600, 0] = 1e-5
        owner = np.zeros(frames, dtype=np.int64)
        gap_mask = np.zeros(frames, dtype=bool)
        seg = FakeSeg("D1", "S1")
        result = measure(timeline, owner, gap_mask,
                         [contribution(0, frames - 1, seg)],
                         48000, 100, -70.0, -10.0, "drop")
        self.assertEqual(result["window_count"], 2)
        self.assertTrue(result["windows"][0]["passes_absolute"])
        self.assertFalse(result["windows"][1]["passes_absolute"])
        self.assertEqual(result["windows_passed_relative"], 1)
        self.assertAlmostEqual(result["integrated_energy"], 0.25)
        self.assertEqual(
            result["windows"][0]["attribution"][0]["segment_id"], "S1")

    def test_all_silent_deterministic(self):
        frames = 4800
        timeline = np.zeros((frames, 2))
        owner = np.full(frames, -1, dtype=np.int64)
        gap_mask = np.ones(frames, dtype=bool)
        result = measure(timeline, owner, gap_mask, [],
                         48000, 100, -70.0, -10.0, "include")
        self.assertTrue(result["all_silent"])
        self.assertEqual(result["integrated_energy"], 0.0)
        self.assertEqual(result["integrated_loudness_dbfs"], float("-inf"))
        self.assertEqual(result["windows_passed_relative"], 0)


if __name__ == "__main__":
    unittest.main()

