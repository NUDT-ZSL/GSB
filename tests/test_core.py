from __future__ import annotations

import unittest

import numpy as np

from vocal_feedback.audio import AudioData, frame_audio
from vocal_feedback.config import Config
from vocal_feedback.dataset import discover_samples
from vocal_feedback.lyrics import parse_lyrics_text, split_pinyin
from vocal_feedback.pitch import (
    estimate_f0,
    hz_to_midi,
    midi_to_hz,
    midi_to_note_name,
    note_name_to_midi,
    smooth_and_correct_pitch,
)


class TestPitchConversions(unittest.TestCase):
    def test_midi_round_trip(self) -> None:
        self.assertAlmostEqual(note_name_to_midi("A4"), 69.0)
        self.assertAlmostEqual(note_name_to_midi("C#4"), 61.0)
        self.assertAlmostEqual(note_name_to_midi("Bb3"), 58.0)
        midi = hz_to_midi(440.0)
        self.assertAlmostEqual(midi, 69.0)
        self.assertAlmostEqual(midi_to_hz(midi), 440.0)
        self.assertEqual(midi_to_note_name(69.0), "A4")
        self.assertIsNone(hz_to_midi(None))


class TestLyrics(unittest.TestCase):
    def test_chinese_syllables_and_phonemes(self) -> None:
        lyrics = parse_lyrics_text("春风吹过小河旁\n白云落在青山上")
        self.assertEqual(len(lyrics.syllables), 14)
        self.assertEqual([len(phrase.syllable_indices) for phrase in lyrics.phrases], [7, 7])
        self.assertEqual(split_pinyin("chun"), ("ch", "un"))
        first = lyrics.syllables[0]
        self.assertEqual(first.text, "春")
        self.assertEqual(first.initial, "ch")
        self.assertEqual(first.final, "un")
        self.assertEqual([phoneme.kind for phoneme in first.phonemes], ["consonant", "vowel"])


def _pitch_frame(index: int, midi: float | None) -> dict[str, object]:
    frequency = midi_to_hz(midi) if midi is not None else None
    return {
        "index": index,
        "timestamp_s": index * 0.01,
        "f0_hz": frequency,
        "midi": midi,
        "voiced": midi is not None,
        "voicing_confidence": 0.8 if midi is not None else 0.0,
    }


class TestSmoothing(unittest.TestCase):
    def test_isolated_octave_error_is_corrected_and_raw_is_kept(self) -> None:
        values = [60.0, 60.05, 60.0, 72.0, 60.02, 60.0, 60.03]
        frames = [_pitch_frame(index, value) for index, value in enumerate(values)]
        result = smooth_and_correct_pitch(frames, Config())
        self.assertTrue(result[3]["octave_corrected"])
        self.assertAlmostEqual(float(result[3]["raw_midi"]), 72.0, places=6)
        self.assertLess(abs(float(result[3]["midi"]) - 60.0), 0.2)

    def test_glissando_is_not_collapsed_to_one_pitch(self) -> None:
        values = [60.0 + index * 0.18 for index in range(24)]
        frames = [_pitch_frame(index, value) for index, value in enumerate(values)]
        result = smooth_and_correct_pitch(frames, Config())
        smoothed = [float(frame["midi"]) for frame in result]
        self.assertGreater(max(smoothed) - min(smoothed), 3.0)
        self.assertLessEqual(min(int(frame["smoothing_window"]) for frame in result), 7)


class TestPitchEstimator(unittest.TestCase):
    def _estimate(self, frequency: float) -> list[dict[str, object]]:
        sample_rate = 16000
        duration = 0.45
        time = np.arange(int(sample_rate * duration)) / sample_rate
        signal = np.zeros_like(time)
        for harmonic in range(1, 8):
            if frequency * harmonic < sample_rate * 0.45:
                signal += np.sin(2.0 * np.pi * frequency * harmonic * time) / harmonic
        signal *= 0.15 / (np.max(np.abs(signal)) + 1e-12)
        signal[: int(0.03 * sample_rate)] *= np.linspace(0.0, 1.0, int(0.03 * sample_rate))
        audio = AudioData(signal, sample_rate, duration, "<synthetic>")
        framed = frame_audio(audio, 0.04, 0.01)
        return estimate_f0(framed, Config())

    def test_low_and_high_notes_are_voiced_near_target(self) -> None:
        for frequency in (180.0, 520.0):
            frames = self._estimate(frequency)
            voiced = [
                frame
                for frame in frames
                if frame["voiced"] and frame["f0_hz"] is not None
            ]
            self.assertGreater(len(voiced), len(frames) * 0.55)
            measured = float(np.median([float(frame["f0_hz"]) for frame in voiced]))
            self.assertLess(abs(measured - frequency) / frequency, 0.035)
            self.assertTrue(
                all(
                    frame["method"] in {"autocorrelation", "cepstrum"}
                    for frame in voiced
                )
            )
            self.assertTrue(
                all("selection_reason" in frame and "voicing_confidence" in frame for frame in frames)
            )

    def test_silence_has_null_pitch_not_zero(self) -> None:
        sample_rate = 16000
        audio = AudioData(
            np.zeros(sample_rate // 2, dtype=np.float64),
            sample_rate,
            0.5,
            "<silence>",
        )
        frames = estimate_f0(frame_audio(audio, 0.04, 0.01), Config())
        self.assertTrue(all(frame["voiced"] is False for frame in frames))
        self.assertTrue(all(frame["f0_hz"] is None for frame in frames))
        self.assertTrue(all(frame["midi"] is None for frame in frames))


class TestDatasetDiscovery(unittest.TestCase):
    def test_generated_samples_are_discovered(self) -> None:
        samples = discover_samples("samples")
        self.assertGreaterEqual(len(samples), 3)
        self.assertTrue(all(sample.lyrics.is_file() for sample in samples))


if __name__ == "__main__":
    unittest.main()

