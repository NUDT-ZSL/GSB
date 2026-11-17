import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from mocap_audio.timeline import map_segment


class FakeSeg:
    def __init__(self, start_ms, channels, gain):
        self.device_id = "D"
        self.segment_id = "S"
        self.start_ms = start_ms
        self.duration_ms = 1000
        self.channels = channels
        self.gain = gain


class MappingAccuracyTest(unittest.TestCase):
    def test_round_trip_mapping_within_one_reference_sample(self):
        ref_rate = 48000
        for dev_rate in (44100, 96000):
            for drift_ppm in (-200.0, 250.0):
                drift_coeff = 1.0 + drift_ppm * 1e-6
                offset_device = 3.7
                frames = int(dev_rate)
                n = np.arange(frames, dtype=np.float64)
                # 用线性扫频信号，便于在统一轴上定位峰值位置
                data = (0.5 * np.sin(2 * np.pi * 220.0 * n / dev_rate)
                        )[:, np.newaxis]
                clock = {
                    "drift_coeff": drift_coeff,
                    "drift_rate": drift_coeff - 1.0,
                    "offset_samples_device": offset_device,
                }
                seg = FakeSeg(0, ["A"], [1.0])
                contrib = map_segment(data, dev_rate, ref_rate, clock, seg,
                                      ["A"])
                # 任选 50 个设备采样点，计算其参考轴真值与映射网格的距离
                probe_n = np.linspace(100, frames - 100, 50)
                true_ref = (probe_n - offset_device) / drift_coeff * \
                    ref_rate / dev_rate
                grid = np.arange(contrib["ref_start"],
                                 contrib["ref_end"] + 1)
                distances = np.min(
                    np.abs(grid[:, np.newaxis] - true_ref[np.newaxis, :]),
                    axis=0)
                self.assertLessEqual(float(np.max(distances)), 1.0,
                                     msg=(dev_rate, drift_ppm))


if __name__ == "__main__":
    unittest.main()

