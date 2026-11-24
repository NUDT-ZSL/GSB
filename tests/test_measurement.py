from __future__ import annotations

import unittest

import numpy as np

from audio_pipeline.measurement import compute_window_records


class MeasurementAlgorithmTests(unittest.TestCase):
    def test_tail_policies_align_to_sample_windows(self) -> None:
        samples = np.full((1100, 1), 0.25, dtype=np.float32)
        weights = (1.0,)
        zero_pad = compute_window_records(samples, weights, 400, 200, "zero_pad")
        shortened = compute_window_records(samples, weights, 400, 200, "shorten")
        dropped = compute_window_records(samples, weights, 400, 200, "drop")

        self.assertEqual(len(zero_pad), 5)
        self.assertEqual(len(shortened), 5)
        self.assertEqual(len(dropped), 4)
        self.assertEqual(
            [(item["start_frame"], item["end_frame"]) for item in zero_pad],
            [(0, 400), (200, 600), (400, 800), (600, 1000), (800, 1100)],
        )
        self.assertTrue(zero_pad[-1]["partial"])
        self.assertEqual(zero_pad[-1]["valid_frames"], 300)
        self.assertEqual(zero_pad[-1]["divisor_frames"], 400)
        self.assertLess(zero_pad[-1]["energy"], shortened[-1]["energy"])

    def test_channel_weights_are_normalized(self) -> None:
        samples = np.zeros((400, 2), dtype=np.float32)
        samples[:, 0] = 0.5
        samples[:, 1] = 0.0
        records = compute_window_records(samples, (0.75, 0.25), 400, 400, "drop")
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0]["energy"], 0.75 * 0.25, places=12)


if __name__ == "__main__":
    unittest.main()
