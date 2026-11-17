import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from mocap_audio.errors import SyncError
from mocap_audio.session import PulseSpec
from mocap_audio.sync import detect_peaks, estimate_clock
from tests._helpers import make_tone, stamp_pulse


def build_clock_data(rate, pulse_ms_list, offset_ms, drift_ppm, missing=()):
    frames = int(rate)
    data = make_tone(frames, 2, seed=rate)
    offset_samples = offset_ms * rate / 1000.0
    drift = 1.0 + drift_ppm * 1e-6
    pulses = []
    for idx, ms in enumerate(pulse_ms_list):
        theory = ms * rate / 1000.0
        observed = drift * theory + offset_samples
        if idx not in missing:
            stamp_pulse(data, int(round(observed)))
        pulses.append(PulseSpec(ms, 5))
    return data, pulses


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_linear_estimate_recovers_clock(self):
        data, pulses = build_clock_data(
            44100, [80, 240, 400, 560, 720], offset_ms=2.0, drift_ppm=150.0)
        observations = detect_peaks(data, 44100, pulses)
        self.assertTrue(all(o["found"] for o in observations))
        result = estimate_clock(observations, 44100, 48000, 1,
                                {"device_id": "D", "segment_id": "S"})
        self.assertFalse(result["degraded"])
        self.assertAlmostEqual(result["drift_ppm"], 150.0, delta=20.0)
        expected_offset = 2.0 * 44100 / 1000.0
        self.assertLess(abs(result["offset_samples_device"] - expected_offset),
                        1.0)
        self.assertLess(result["max_abs_residual_device_samples"], 1.0)
        self.assertEqual(len(result["observations"]), 5)

    def test_degradation_to_offset_only(self):
        data, pulses = build_clock_data(
            48000, [100, 300], offset_ms=-1.0, drift_ppm=0.0, missing=(1,))
        observations = detect_peaks(data, 48000, pulses)
        result = estimate_clock(observations, 48000, 48000, 1,
                                {"device_id": "D", "segment_id": "S"})
        self.assertTrue(result["degraded"])
        self.assertEqual(result["used_model_order"], 0)
        self.assertEqual(result["observation_count"], 1)
        self.assertEqual(len(result["missing_markers"]), 1)
        self.assertTrue(result["degradation_reasons"])

    def test_no_observation_raises(self):
        data, pulses = build_clock_data(
            48000, [100, 300], offset_ms=0.0, drift_ppm=0.0, missing=(0, 1))
        observations = detect_peaks(data, 48000, pulses)
        with self.assertRaises(SyncError) as ctx:
            estimate_clock(observations, 48000, 48000, 1,
                           {"device_id": "D", "segment_id": "S"})
        self.assertEqual(ctx.exception.code, "E_SYNC")
        self.assertFalse(ctx.exception.degraded)

    def test_search_window_out_of_segment(self):
        data = make_tone(4800, 1)
        pulses = [PulseSpec(150, 10)]
        observations = detect_peaks(data, 48000, pulses)
        self.assertFalse(observations[0]["found"])
        self.assertTrue(observations[0]["out_of_segment"])


if __name__ == "__main__":
    unittest.main()

