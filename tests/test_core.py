from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np

from audio_qa.framing import frame_count, iter_frames
from audio_qa.cli import parse_sample_count
from audio_qa.generator import _add_impulses, _synth_music, generate_dataset
from audio_qa.grading import (
    critical_conditions,
    grade_clipping,
    grade_dc_offset,
    grade_impulse,
    grade_noise,
)
from audio_qa.impulses import detect_impulses
from audio_qa.noise import track_noise_floor
from audio_qa.wav import read_mono_wav, write_mono_wav


class FramingTests(unittest.TestCase):
    def test_parameter_unit_parsing_and_fractional_flag(self):
        samples, request = parse_sample_count("0.032s", 16000)
        self.assertEqual(samples, 512)
        self.assertFalse(request["fractional"])
        samples, request = parse_sample_count("32ms", 16000)
        self.assertEqual(samples, 512)
        samples, request = parse_sample_count("0.0321", 16000)
        self.assertEqual(samples, 514)
        self.assertTrue(request["fractional"])

    def test_segment_scoped_and_incomplete_suffix(self):
        frames = list(iter_frames(100, 1000, frame_length=64, hop=32))
        self.assertEqual(len(frames), frame_count(100, 1000, 32))
        self.assertEqual(frames[0].start, 100)
        self.assertEqual(frames[-1].end, 1000)
        self.assertTrue(all(100 <= f.start < f.end <= 1000 for f in frames))
        incomplete = [index for index, f in enumerate(frames) if not f.complete]
        self.assertTrue(incomplete)
        self.assertEqual(incomplete, list(range(incomplete[0], len(frames))))
        self.assertTrue(all(f.end == 1000 for f in frames[incomplete[0] :]))

    def test_no_cross_segment(self):
        first = list(iter_frames(0, 500, 64, 32))
        second = list(iter_frames(500, 1000, 64, 32))
        self.assertTrue(all(f.end <= 500 for f in first))
        self.assertTrue(all(f.start >= 500 for f in second))
        self.assertEqual(second[0].start, 500)


class WavTests(unittest.TestCase):
    def test_mono_roundtrip(self):
        sample_rate = 16000
        t = np.arange(sample_rate, dtype=np.float64) / sample_rate
        signal = 0.3 * np.sin(2 * np.pi * 440 * t)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            write_mono_wav(path, signal, sample_rate)
            loaded, loaded_sr = read_mono_wav(path)
        self.assertEqual(loaded_sr, sample_rate)
        self.assertEqual(loaded.size, signal.size)
        self.assertLess(float(np.max(np.abs(loaded - signal))), 1.0e-4)


class GradingTests(unittest.TestCase):
    def test_non_overlapping_boundaries(self):
        self.assertEqual(grade_clipping(0.019999), "light")
        self.assertEqual(grade_clipping(0.02), "medium")
        self.assertEqual(grade_clipping(0.149999), "medium")
        self.assertEqual(grade_clipping(0.15), "heavy")

        self.assertEqual(grade_dc_offset(0.039999), "light")
        self.assertEqual(grade_dc_offset(0.04), "medium")
        self.assertEqual(grade_dc_offset(0.12), "heavy")

        self.assertEqual(grade_noise(-56.001), "light")
        self.assertEqual(grade_noise(-56.0), "medium")
        self.assertEqual(grade_noise(-46.0), "heavy")

        self.assertEqual(grade_impulse(0.399, 0.299)[0], "light")
        self.assertEqual(grade_impulse(0.4, 0.0)[0], "medium")
        self.assertEqual(grade_impulse(0.0, 0.3)[0], "medium")
        self.assertEqual(grade_impulse(2.0, 0.0)[0], "heavy")
        self.assertEqual(grade_impulse(0.0, 0.6)[0], "heavy")

    def test_critical_band_condition_reported(self):
        flags = critical_conditions("segment_clip_coverage", 0.0205, [(0.02, 0.003)])
        self.assertEqual(len(flags), 1)
        self.assertIn("临界带", flags[0]["condition"])


class ImpulseAndNoiseTests(unittest.TestCase):
    def test_clean_music_no_impulse_but_added_click_detected(self):
        sample_rate = 16000
        rng = np.random.default_rng(77)
        clean = _synth_music(3 * sample_rate, sample_rate, rng)
        clean_events, _, _ = detect_impulses(clean, sample_rate)
        self.assertEqual(clean_events, [])

        noisy = clean.copy()
        events = _add_impulses(
            noisy,
            sample_rate,
            np.random.default_rng(78),
            {"count": 3, "peak": 0.40},
            forbidden=[],
        )
        self.assertGreaterEqual(len(events), 1)
        detected, _, _ = detect_impulses(noisy, sample_rate)
        self.assertGreaterEqual(len(detected), 1)

    def test_noise_tracker_holds_through_clipped_frames(self):
        frames = []
        for index in range(12):
            frames.append(
                {
                    "frame_index": index,
                    "complete": True,
                    "low_quantile_dbfs": -72.0 if index == 0 else -20.0,
                    "clipping_label": "none" if index == 0 else "hard",
                    "impulse_candidate": False,
                    "noise_floor_updated": False,
                }
            )
        track_noise_floor(frames, hop=128, sample_rate=16000)
        self.assertAlmostEqual(frames[0]["noise_floor_dbfs"], -72.0, places=6)
        self.assertTrue(all(abs(frame["noise_floor_dbfs"] + 72.0) < 1e-6 for frame in frames))
        self.assertFalse(any(frame["noise_floor_updated"] for frame in frames[1:]))


class GeneratorDeterminismTests(unittest.TestCase):
    def test_same_seed_same_bytes(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            manifest_a = generate_dataset(a, seed=12345)
            manifest_b = generate_dataset(b, seed=12345)
            self.assertEqual(
                (Path(a) / "manifest.json").read_bytes(),
                (Path(b) / "manifest.json").read_bytes(),
            )
            for recording in manifest_a["recordings"]:
                hash_a = hashlib.sha256(
                    (Path(a) / recording["path"]).read_bytes()
                ).hexdigest()
                hash_b = hashlib.sha256(
                    (Path(b) / recording["path"]).read_bytes()
                ).hexdigest()
                self.assertEqual(hash_a, hash_b)
            self.assertEqual(
                [r["recording_id"] for r in manifest_a["recordings"]],
                [r["recording_id"] for r in manifest_b["recordings"]],
            )


if __name__ == "__main__":
    unittest.main()
