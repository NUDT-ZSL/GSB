#!/usr/bin/env python3
"""Standard-library test suite for podcast_concat.py.

Every fixture is generated deterministically at runtime inside a temporary
directory.  Run with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import array
import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import podcast_concat as pc  # noqa: E402

SAMPLE_RATE = 44100
PEAK_TARGET_AMPLITUDE = 32767.0 * (10.0 ** (-1.0 / 20.0))


def write_wav(
    path: Path,
    frames: int,
    sample_rate: int = SAMPLE_RATE,
    channels: int = 1,
    sample_width: int = 2,
    amplitude: int = 1000,
) -> None:
    """Write a deterministic, non-silent mono/square-ish test tone.

    The generated samples alternate between +amplitude and -amplitude so the
    maximum absolute sample value is exactly ``amplitude``.
    """
    if sample_width == 2:
        samples = array.array(
            "h", [amplitude if index % 2 == 0 else -amplitude for index in range(frames)]
        )
        payload = samples.tobytes() * channels
    else:
        # Only used to test rejection of non-16-bit files.
        payload = bytes(
            (amplitude % 256 if index % 2 == 0 else 128)
            for index in range(frames * sample_width * channels)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(payload)


def write_silent_wav(path: Path, frames: int, sample_rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames)


def read_wav(path: Path) -> tuple[int, int, int, array.array]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()
    return channels, width, rate, samples


def write_manifest(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast_tests_")
        self.root = Path(self._tmp.name)
        self.manifest_path = self.root / "manifest.json"
        self.output_dir = self.root / "out"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def manifest(self, segments: list[dict], rate: int = SAMPLE_RATE, gap_ms: int = 0) -> Path:
        return write_manifest(
            self.manifest_path,
            {"target_sample_rate": rate, "gap_ms": gap_ms, "segments": segments},
        )

    def run_cli(self, *extra: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = pc.main(
                [
                    "--manifest",
                    str(self.manifest_path),
                    "--output-dir",
                    str(self.output_dir),
                    *extra,
                ]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def load_report(self) -> dict:
        return json.loads((self.output_dir / pc.REPORT_NAME).read_text(encoding="utf-8"))


class TestManifestParsing(BaseCase):
    # Requirement 1
    def test_three_valid_segments_parse(self) -> None:
        for name in ("a.wav", "b.wav", "c.wav"):
            write_wav(self.root / name, SAMPLE_RATE // 10)
        self.manifest(
            [
                {"file": "a.wav", "start_ms": 0, "end_ms": 100},
                {"file": "b.wav", "start_ms": 0, "end_ms": 100},
                {"file": "c.wav", "start_ms": 0, "end_ms": 100},
            ]
        )
        manifest = pc.load_manifest(self.manifest_path)
        self.assertEqual(len(manifest.segments), 3)

        code, _, stderr = self.run_cli()
        self.assertEqual(code, 0, stderr)
        self.assertTrue((self.output_dir / "output.wav").is_file())

    # Requirement 12
    def test_invalid_manifests_are_rejected_before_output(self) -> None:
        write_wav(self.root / "a.wav", SAMPLE_RATE // 10)
        bad_payloads = {
            "missing target_sample_rate": {
                "gap_ms": 0,
                "segments": [{"file": "a.wav", "start_ms": 0, "end_ms": 100}],
            },
            "missing file field": {
                "target_sample_rate": SAMPLE_RATE,
                "gap_ms": 0,
                "segments": [{"start_ms": 0, "end_ms": 100}],
            },
            "non-integer time": {
                "target_sample_rate": SAMPLE_RATE,
                "gap_ms": 0,
                "segments": [{"file": "a.wav", "start_ms": 0, "end_ms": 10.5}],
            },
            "negative start": {
                "target_sample_rate": SAMPLE_RATE,
                "gap_ms": 0,
                "segments": [{"file": "a.wav", "start_ms": -10, "end_ms": 100}],
            },
            "start equals end": {
                "target_sample_rate": SAMPLE_RATE,
                "gap_ms": 0,
                "segments": [{"file": "a.wav", "start_ms": 100, "end_ms": 100}],
            },
            "start beyond end": {
                "target_sample_rate": SAMPLE_RATE,
                "gap_ms": 0,
                "segments": [{"file": "a.wav", "start_ms": 200, "end_ms": 100}],
            },
            "not json": "not a valid manifest",
            "segments not an array": {
                "target_sample_rate": SAMPLE_RATE,
                "gap_ms": 0,
                "segments": {},
            },
        }
        for label, payload in bad_payloads.items():
            with self.subTest(label):
                if isinstance(payload, str):
                    self.manifest_path.write_text(payload, encoding="utf-8")
                else:
                    self.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
                if self.output_dir.exists():
                    for stale in self.output_dir.iterdir():
                        stale.unlink()
                code, _, stderr = self.run_cli()
                self.assertNotEqual(code, 0, label)
                self.assertTrue(stderr.strip(), label)
                self.assertFalse((self.output_dir / "output.wav").exists(), label)


class TestSegmentValidation(BaseCase):
    # Requirement 2
    def test_missing_file_is_error(self) -> None:
        write_wav(self.root / "a.wav", SAMPLE_RATE // 10)
        self.manifest(
            [
                {"file": "a.wav", "start_ms": 0, "end_ms": 100},
                {"file": "missing.wav", "start_ms": 0, "end_ms": 100},
            ]
        )
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        report = self.load_report()
        self.assertEqual(report["segments"][1]["status"], "error")
        self.assertTrue(report["segments"][1]["error"])
        self.assertEqual(report["segments"][0]["status"], "ok")
        self.assertIsNone(report["segments"][0]["error"])

    # Requirement 2
    def test_sample_rate_mismatch_is_error(self) -> None:
        write_wav(self.root / "a.wav", 48000 // 10, sample_rate=48000)
        self.manifest([{"file": "a.wav", "start_ms": 0, "end_ms": 100}])
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        segment = self.load_report()["segments"][0]
        self.assertEqual(segment["status"], "error")
        self.assertIn("sample rate", segment["error"])
        # Error-only run still produces a readable, zero-length WAV.
        self.assertEqual(read_wav(self.output_dir / "output.wav")[0], 1)

    # Requirement 2 (format checks beyond bare sample-rate)
    def test_stereo_and_8bit_are_errors(self) -> None:
        write_wav(self.root / "stereo.wav", SAMPLE_RATE // 10, channels=2)
        write_wav(self.root / "eightbit.wav", SAMPLE_RATE // 10, sample_width=1, amplitude=20)
        self.manifest(
            [
                {"file": "stereo.wav", "start_ms": 0, "end_ms": 100},
                {"file": "eightbit.wav", "start_ms": 0, "end_ms": 100},
            ]
        )
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        report = self.load_report()
        self.assertTrue(all(item["status"] == "error" for item in report["segments"]))
        self.assertIn("mono", report["segments"][0]["error"])
        self.assertIn("16-bit", report["segments"][1]["error"])


class TestExtractionAndConcatenation(BaseCase):
    # Requirement 3
    def test_range_is_clipped_to_file_end_and_warned(self) -> None:
        write_wav(self.root / "a.wav", SAMPLE_RATE)  # exactly 1000 ms
        self.manifest([{"file": "a.wav", "start_ms": 800, "end_ms": 1200}])
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        report = self.load_report()
        segment = report["segments"][0]
        self.assertEqual(segment["status"], "warning")
        self.assertIsNone(segment["error"])
        self.assertTrue(segment["warning"])
        self.assertEqual(segment["duration_ms"], 200)
        self.assertEqual(report["total_duration_ms"], 200)
        # Audio was actually kept: 200 ms of non-zero samples.
        _, _, _, samples = read_wav(self.output_dir / "output.wav")
        self.assertEqual(len(samples), SAMPLE_RATE // 5)
        self.assertTrue(any(samples))

    # Requirement 4
    def test_gap_is_inserted_between_segments(self) -> None:
        write_wav(self.root / "a.wav", SAMPLE_RATE // 10, amplitude=1000)
        write_wav(self.root / "b.wav", SAMPLE_RATE // 10, amplitude=500)
        self.manifest(
            [
                {"file": "a.wav", "start_ms": 0, "end_ms": 100},
                {"file": "b.wav", "start_ms": 0, "end_ms": 100},
            ],
            gap_ms=50,
        )
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        report = self.load_report()
        self.assertEqual(report["total_duration_ms"], 250)
        _, _, _, samples = read_wav(self.output_dir / "output.wav")
        self.assertEqual(len(samples), SAMPLE_RATE // 4)
        # The gap samples in the middle are exactly zero.
        gap_start = SAMPLE_RATE // 10
        gap_end = gap_start + SAMPLE_RATE * 50 // 1000
        self.assertEqual(set(samples[gap_start:gap_end]), {0})

    # Requirement 7
    def test_error_segment_is_skipped_and_later_audio_continues(self) -> None:
        write_wav(self.root / "a.wav", SAMPLE_RATE // 10, amplitude=1000)
        write_wav(self.root / "c.wav", SAMPLE_RATE // 10, amplitude=1000)
        self.manifest(
            [
                {"file": "a.wav", "start_ms": 0, "end_ms": 100},
                {"file": "missing.wav", "start_ms": 0, "end_ms": 100},
                {"file": "c.wav", "start_ms": 0, "end_ms": 100},
            ],
            gap_ms=50,
        )
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        report = self.load_report()
        self.assertEqual([item["status"] for item in report["segments"]], ["ok", "error", "ok"])
        self.assertEqual(report["total_duration_ms"], 250)
        self.assertEqual(report["counts"]["error"], 1)
        self.assertEqual(report["total_frames"], SAMPLE_RATE // 4)


class TestNormalizationAndOutput(BaseCase):
    # Requirement 5
    def test_peak_normalization_default_target(self) -> None:
        write_wav(self.root / "a.wav", SAMPLE_RATE // 10, amplitude=1234)
        self.manifest([{"file": "a.wav", "start_ms": 0, "end_ms": 100}])
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        _, _, _, samples = read_wav(self.output_dir / "output.wav")
        output_peak = max(abs(value) for value in samples)
        self.assertLessEqual(abs(output_peak - PEAK_TARGET_AMPLITUDE), 1.0)
        report = self.load_report()
        self.assertAlmostEqual(report["target_peak_db"], -1.0)
        self.assertGreater(report["applied_gain"], 1.0)

    # Requirement 5 (custom target; same gain for everyone)
    def test_custom_target_peak_db(self) -> None:
        write_wav(self.root / "a.wav", SAMPLE_RATE // 10, amplitude=2000)
        self.manifest([{"file": "a.wav", "start_ms": 0, "end_ms": 100}])
        code, _, _ = self.run_cli("--target-peak-db", "-6")
        self.assertEqual(code, 0)
        _, _, _, samples = read_wav(self.output_dir / "output.wav")
        expected = 32767.0 * (10.0 ** (-6.0 / 20.0))
        self.assertLessEqual(abs(max(abs(v) for v in samples) - expected), 1.0)

    # Requirement 8
    def test_output_wav_header(self) -> None:
        write_wav(self.root / "a.wav", SAMPLE_RATE // 10)
        self.manifest([{"file": "a.wav", "start_ms": 0, "end_ms": 100}], rate=SAMPLE_RATE)
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        channels, width, rate, _ = read_wav(self.output_dir / "output.wav")
        self.assertEqual((channels, width, rate), (1, 2, SAMPLE_RATE))

    # Requirement 9
    def test_all_rate_mismatched_segments_yield_zero_length_wav(self) -> None:
        write_wav(self.root / "a.wav", 48000 // 10, sample_rate=48000)
        write_wav(self.root / "b.wav", 48000 // 10, sample_rate=48000)
        self.manifest(
            [
                {"file": "a.wav", "start_ms": 0, "end_ms": 100},
                {"file": "b.wav", "start_ms": 0, "end_ms": 100},
            ],
            rate=44100,
        )
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        report = self.load_report()
        self.assertEqual(report["total_duration_ms"], 0)
        self.assertEqual(report["total_frames"], 0)
        self.assertTrue(all(item["status"] == "error" for item in report["segments"]))
        self.assertTrue(all(item["error"] for item in report["segments"]))
        channels, width, rate, samples = read_wav(self.output_dir / "output.wav")
        self.assertEqual((channels, width, rate), (1, 2, 44100))
        self.assertEqual(len(samples), 0)

    # Requirement 13
    def test_all_silent_input_stays_silent_with_warning(self) -> None:
        write_silent_wav(self.root / "silent.wav", SAMPLE_RATE // 10)
        self.manifest([{"file": "silent.wav", "start_ms": 0, "end_ms": 100}])
        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 0, stderr)
        report = self.load_report()
        self.assertTrue(report["silent"])
        self.assertTrue(report["warnings"])
        self.assertTrue(any("silent" in warning for warning in report["warnings"]))
        _, _, _, samples = read_wav(self.output_dir / "output.wav")
        self.assertEqual(len(samples), SAMPLE_RATE // 10)
        self.assertEqual(set(samples), {0})
        self.assertIn("Processed silent.wav: ok", stdout)


class TestCliBehaviour(BaseCase):
    def _three_segments(self) -> None:
        for name in ("a.wav", "b.wav", "c.wav"):
            write_wav(self.root / name, SAMPLE_RATE // 10, amplitude=1000)

    # Requirement 10
    def test_stdout_lines_follow_manifest_order(self) -> None:
        self._three_segments()
        self.manifest(
            [
                {"file": "a.wav", "start_ms": 0, "end_ms": 100},
                {"file": "missing.wav", "start_ms": 0, "end_ms": 100},
                {"file": "c.wav", "start_ms": 0, "end_ms": 100},
            ]
        )
        code, stdout, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(
            stdout.splitlines(),
            ["Processed a.wav: ok", "Processed missing.wav: error", "Processed c.wav: ok"],
        )

    # Requirement 11
    def test_gap_ms_cli_override(self) -> None:
        self._three_segments()
        self.manifest(
            [
                {"file": "a.wav", "start_ms": 0, "end_ms": 100},
                {"file": "b.wav", "start_ms": 0, "end_ms": 100},
                {"file": "c.wav", "start_ms": 0, "end_ms": 100},
            ],
            gap_ms=0,
        )
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        default_total = self.load_report()["total_duration_ms"]
        self.assertEqual(default_total, 300)

        code, _, _ = self.run_cli("--gap-ms", "100")
        self.assertEqual(code, 0)
        overridden_total = self.load_report()["total_duration_ms"]
        self.assertEqual(overridden_total - default_total, 200)  # 2 gaps x 100 ms

    # Requirement 11
    def test_manifest_gap_ms_used_by_default(self) -> None:
        write_wav(self.root / "a.wav", SAMPLE_RATE // 10)
        write_wav(self.root / "b.wav", SAMPLE_RATE // 10)
        self.manifest(
            [
                {"file": "a.wav", "start_ms": 0, "end_ms": 100},
                {"file": "b.wav", "start_ms": 0, "end_ms": 100},
            ],
            gap_ms=50,
        )
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.load_report()["gap_ms"], 50)
        self.assertEqual(self.load_report()["total_duration_ms"], 250)

    # Requirement 14
    def test_output_directory_is_created(self) -> None:
        write_wav(self.root / "a.wav", SAMPLE_RATE // 10)
        self.manifest([{"file": "a.wav", "start_ms": 0, "end_ms": 100}])
        self.output_dir = self.root / "nested" / "deep" / "out"
        self.assertFalse(self.output_dir.exists())
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertTrue((self.output_dir / "output.wav").is_file())
        self.assertTrue((self.output_dir / "report.json").is_file())

    # Requirement 6
    def test_report_shape(self) -> None:
        write_wav(self.root / "a.wav", SAMPLE_RATE // 10)
        write_wav(self.root / "b.wav", SAMPLE_RATE // 10)
        self.manifest(
            [
                {"file": "a.wav", "start_ms": 0, "end_ms": 100},
                {"file": "missing.wav", "start_ms": 0, "end_ms": 100},
                {"file": "b.wav", "start_ms": 0, "end_ms": 100},
            ],
            gap_ms=50,
        )
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        report = self.load_report()
        self.assertEqual(len(report["segments"]), 3)
        self.assertIn("output.wav", report["output_path"])
        self.assertEqual(report["total_duration_ms"], 250)
        for segment in report["segments"]:
            self.assertIn(segment["status"], {"ok", "warning", "error"})
            self.assertIn("error", segment)
            self.assertIn("duration_ms", segment)
            self.assertIn("file", segment)
        self.assertEqual(report["segments"][0]["error"], None)
        self.assertIsNotNone(report["segments"][1]["error"])
        self.assertEqual(report["segments"][2]["error"], None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
