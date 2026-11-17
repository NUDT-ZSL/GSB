import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from mocap_audio.resample import rational_resample, theoretical_output_frames


class ResampleTest(unittest.TestCase):
    def test_frame_counts_rational_rates(self):
        for src, dst in ((48000, 44100), (44100, 48000), (48000, 96000),
                         (96000, 44100)):
            frames = 12345
            samples = np.random.RandomState(frames).uniform(
                -0.5, 0.5, size=(frames, 2))
            out = rational_resample(samples, src, dst)
            expected = theoretical_output_frames(frames, src, dst)
            self.assertEqual(out.shape[0], expected)
            self.assertLessEqual(abs(out.shape[0] - frames * dst / src), 1.0)

    def test_identity_resample(self):
        samples = np.zeros((100, 3))
        out = rational_resample(samples, 48000, 48000)
        self.assertEqual(out.shape, samples.shape)

    def test_dc_signal_preserved(self):
        samples = np.full((4800, 1), 0.37)
        out = rational_resample(samples, 48000, 44100)
        self.assertTrue(np.all(np.abs(out - 0.37) < 1e-9))


if __name__ == "__main__":
    unittest.main()

