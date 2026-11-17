from __future__ import annotations

import math
import unittest
import wave
from pathlib import Path

from voice_dialog.audio import decode_audio, scan_audio_directory
from voice_dialog.constants import BALANCED_FEATURES, COMPACT_FEATURES, E_AUDIO_FORMAT, E_NONFINITE_SAMPLE, E_ZERO_DURATION
from voice_dialog.errors import BusinessError
from voice_dialog.features import extract_audio_features
from voice_dialog.synthetic import _write_wav, synthesize_clip

from tests.helpers import TempCase


class FeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = TempCase()
        self.dataset = self.case.dataset("features", segments=4, turns=2, anomalies=False)

    def tearDown(self) -> None:
        self.case.close()

    def _scan_first(self):
        scans = scan_audio_directory(self.dataset / "audio")
        self.assertTrue(scans)
        return scans[0]

    def test_frames_are_deterministic_and_quantized(self) -> None:
        scan = self._scan_first()
        first = extract_audio_features(
            scan,
            frame_length_ms=25,
            frame_shift_ms=10,
            effective_features=BALANCED_FEATURES,
        )
        second = extract_audio_features(
            scan,
            frame_length_ms=25,
            frame_shift_ms=10,
            effective_features=BALANCED_FEATURES,
        )
        self.assertEqual(first, second)
        expected_frames = math.ceil(scan.header.frame_count / 160)
        self.assertEqual(first["frame_count"], expected_frames)
        self.assertEqual(len(first["frames"]), expected_frames)
        self.assertTrue(all(frame["f0_hz"] == 0 or frame["f0_hz"] % 10 == 0 for frame in first["frames"]))
        self.assertTrue(all(frame["index"] == index for index, frame in enumerate(first["frames"])))

    def test_feature_dimension_is_bounded(self) -> None:
        scan = self._scan_first()
        artifact = extract_audio_features(
            scan,
            frame_length_ms=25,
            frame_shift_ms=10,
            effective_features=COMPACT_FEATURES[:5],
        )
        self.assertEqual(len(artifact["effective_features"]), 5)
        self.assertEqual(len(artifact["segment_feature_vector"]), 5)
        self.assertLessEqual(len(artifact["segment_feature_vector"]), len(BALANCED_FEATURES))

    def test_bad_rate_is_scan_failure(self) -> None:
        path = self.dataset / "audio" / "bad_rate.wav"
        signal = synthesize_clip("neutral", seed=1, index=1, duration=0.3, speaker_shift=0)
        _write_wav(path, signal, sample_rate=8000)
        scans = {scan.filename: scan for scan in scan_audio_directory(self.dataset / "audio")}
        self.assertFalse(scans["bad_rate.wav"].valid)
        self.assertEqual(scans["bad_rate.wav"].error_code, E_AUDIO_FORMAT)

    def test_zero_duration_injection(self) -> None:
        path = self.dataset / "audio" / "zero.wav"
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b"")
        scan = next(scan for scan in scan_audio_directory(self.dataset / "audio") if scan.filename == "zero.wav")
        injection = [{"id": "INJ-Z", "kind": "zero_duration", "target": "zero.wav"}]
        with self.assertRaises(BusinessError) as context:
            extract_audio_features(
                scan,
                frame_length_ms=25,
                frame_shift_ms=10,
                effective_features=COMPACT_FEATURES,
                injections=injection,
            )
        self.assertEqual(context.exception.code, E_ZERO_DURATION)
        self.assertEqual(context.exception.injection_id, "INJ-Z")

    def test_nonfinite_injection(self) -> None:
        scan = self._scan_first()
        injection = [{"id": "INJ-N", "kind": "nonfinite_sample", "target": scan.filename}]
        with self.assertRaises(BusinessError) as context:
            extract_audio_features(
                scan,
                frame_length_ms=25,
                frame_shift_ms=10,
                effective_features=COMPACT_FEATURES,
                injections=injection,
            )
        self.assertEqual(context.exception.code, E_NONFINITE_SAMPLE)
        self.assertEqual(context.exception.injection_id, "INJ-N")


if __name__ == "__main__":
    unittest.main()
