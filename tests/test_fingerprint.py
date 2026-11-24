from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from audiofp.config import FingerprintParams
from audiofp.dsp import extract_fingerprint_chunk, frame_count
from audiofp.wavio import parse_wav
from tests.helpers import write_wav


class FingerprintTests(unittest.TestCase):
    def test_chunked_and_concurrent_extraction_are_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rate = 16000
            rng = np.random.default_rng(42)
            time = np.arange(rate, dtype=np.float64) / rate
            signal = (
                0.3 * np.sin(2 * np.pi * 220 * time)
                + 0.2 * np.sin(2 * np.pi * 731 * time)
                + 0.05 * rng.normal(size=len(time))
            )
            path = root / "signal.wav"
            write_wav(path, signal, rate)
            info = parse_wav(path, label="signal.wav")
            params = FingerprintParams(
                frame_length=512,
                hop_length=128,
                band_boundaries_hz=(50.0, 250.0, 600.0, 1200.0, 3000.0, 7000.0),
                peak_neighborhood=3,
                peaks_per_frame=4,
                fan_out_frames=6,
                frequency_bucket_hz=50.0,
                pair_limit_per_peak=12,
            )
            total = frame_count(info.sample_count, 512, 128)
            whole = extract_fingerprint_chunk(info, params, 0, total)
            chunks = [(start, min(total, start + 37)) for start in range(0, total, 37)]
            chunked = []
            for start, end in chunks:
                chunked.extend(extract_fingerprint_chunk(info, params, start, end))
            chunked.sort(
                key=lambda item: (
                    item["anchor_frame"],
                    item["band"],
                    item["delta_frames"],
                    item["target_band"],
                    item["anchor_bin"],
                    item["target_bin"],
                    item["target_frame"],
                )
            )
            self.assertEqual(whole, chunked)

            with ThreadPoolExecutor(max_workers=4) as executor:
                concurrent = list(
                    executor.map(
                        lambda bounds: extract_fingerprint_chunk(
                            info, params, bounds[0], bounds[1]
                        ),
                        chunks,
                    )
                )
            flattened = [item for part in concurrent for item in part]
            flattened.sort(
                key=lambda item: (
                    item["anchor_frame"],
                    item["band"],
                    item["delta_frames"],
                    item["target_band"],
                    item["anchor_bin"],
                    item["target_bin"],
                    item["target_frame"],
                )
            )
            self.assertEqual(whole, flattened)
            for item in whole:
                self.assertEqual(
                    item["anchor_sample_start"], item["anchor_frame"] * 128
                )
                self.assertEqual(
                    item["anchor_sample_end"], item["anchor_frame"] * 128 + 512
                )


if __name__ == "__main__":
    unittest.main()

