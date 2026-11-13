from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.samples import (  # noqa: E402
    constant_wav,
    eight_bit_wav,
    read_all_samples,
    sine_wav,
    write_manifest,
)
from wavmixer.mix import fade_factor, fade_frame_count  # noqa: E402


def run_cli(manifest: Path, output: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "wavmixer", "--manifest", str(manifest), "--out", str(output)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def load_report(output: Path) -> dict:
    report_path = Path(str(output) + ".report.json")
    return json.loads(report_path.read_text(encoding="utf-8"))


class MixerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wavmixer-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wav(self, name: str, **kwargs) -> Path:
        return sine_wav(self.tmp / name, **kwargs)

    def const(self, name: str, **kwargs) -> Path:
        return constant_wav(self.tmp / name, **kwargs)


class TestManifestValidation(MixerTestCase):
    def test_negative_offset_exits_non_zero_with_field_error(self):
        source = self.wav("a.wav", freq_hz=440, duration_ms=20)
        manifest = write_manifest(
            self.tmp / "bad-offset.json",
            [{"id": "a", "source": source.name, "offset_ms": -1, "gain": 1.0}],
        )
        out = self.tmp / "out" / "mix.wav"
        result = run_cli(manifest, out)
        self.assertNotEqual(result.returncode, 0)
        report = load_report(out)
        self.assertEqual(report["status"], "invalid")
        fields = " ".join(error["field"] for error in report["errors"])
        self.assertIn("offset_ms", fields)
        self.assertFalse(out.exists())

    def test_gain_above_range_exits_non_zero_with_field_error(self):
        source = self.wav("a.wav", freq_hz=440, duration_ms=20)
        manifest = write_manifest(
            self.tmp / "bad-gain.json",
            [{"id": "a", "source": source.name, "offset_ms": 0, "gain": 3.0}],
        )
        out = self.tmp / "out" / "mix.wav"
        result = run_cli(manifest, out)
        self.assertNotEqual(result.returncode, 0)
        report = load_report(out)
        fields = " ".join(error["field"] for error in report["errors"])
        self.assertIn("gain", fields)
        self.assertEqual(report["errors"][0]["value"], 3.0)

    def test_non_integer_offset_rejected(self):
        source = self.wav("a.wav", freq_hz=440, duration_ms=20)
        manifest = write_manifest(
            self.tmp / "bad-type.json",
            [{"id": "a", "source": source.name, "offset_ms": 1.5, "gain": 1.0}],
        )
        out = self.tmp / "out" / "mix.wav"
        self.assertNotEqual(run_cli(manifest, out).returncode, 0)
        report = load_report(out)
        self.assertIn("offset_ms", report["errors"][0]["field"])

    def test_all_field_errors_reported_together(self):
        manifest = write_manifest(
            self.tmp / "many.json",
            [
                {"id": "a", "source": "a.wav", "offset_ms": -1, "gain": 1.0},
                {"id": "b", "source": "b.wav", "offset_ms": 0, "gain": 3.0},
                {"id": "c", "source": "c.wav", "offset_ms": True, "gain": -0.5},
            ],
        )
        out = self.tmp / "out" / "mix.wav"
        self.assertNotEqual(run_cli(manifest, out).returncode, 0)
        report = load_report(out)
        fields = [error["field"] for error in report["errors"]]
        self.assertIn("clips[0].offset_ms", fields)
        self.assertIn("clips[1].gain", fields)
        self.assertIn("clips[2].offset_ms", fields)  # True is a bool, not an int
        self.assertIn("clips[2].gain", fields)

    def test_stale_output_removed_when_run_fails(self):
        out = self.tmp / "out" / "mix.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"stale")
        manifest = write_manifest(
            self.tmp / "bad.json",
            [{"id": "a", "source": "missing.wav", "offset_ms": -1, "gain": 1.0}],
        )
        self.assertNotEqual(run_cli(manifest, out).returncode, 0)
        self.assertFalse(out.exists())


class TestSourceFormatValidation(MixerTestCase):
    def test_sample_rate_mismatch_lists_file(self):
        a = self.wav("a.wav", freq_hz=440, duration_ms=20, sample_rate=44100)
        b = self.wav("b.wav", freq_hz=660, duration_ms=20, sample_rate=22050)
        manifest = write_manifest(
            self.tmp / "rate.json",
            [
                {"id": "a", "source": a.name, "offset_ms": 0, "gain": 1.0},
                {"id": "b", "source": b.name, "offset_ms": 5, "gain": 1.0},
            ],
        )
        out = self.tmp / "out" / "mix.wav"
        result = run_cli(manifest, out)
        self.assertNotEqual(result.returncode, 0)
        report = load_report(out)
        self.assertEqual(report["status"], "failed")
        mismatched = " ".join(item["source"] for item in report["format_mismatches"])
        self.assertIn("b.wav", mismatched)
        self.assertFalse(out.exists())

    def test_eight_bit_source_lists_file(self):
        a = self.wav("a.wav", freq_hz=440, duration_ms=20)
        b = eight_bit_wav(self.tmp / "eight.wav")
        manifest = write_manifest(
            self.tmp / "depth.json",
            [
                {"id": "a", "source": a.name, "offset_ms": 0, "gain": 1.0},
                {"id": "b", "source": b.name, "offset_ms": 5, "gain": 1.0},
            ],
        )
        out = self.tmp / "out" / "mix.wav"
        result = run_cli(manifest, out)
        self.assertNotEqual(result.returncode, 0)
        report = load_report(out)
        self.assertEqual(report["reason"], "source_format_invalid")
        messages = " ".join(error["message"] for error in report["errors"])
        sources = " ".join(error["source"] for error in report["errors"])
        self.assertIn("8-bit", messages)
        self.assertIn("eight.wav", sources)


class TestTimelineAndFades(MixerTestCase):
    def test_total_duration_is_max_end_time(self):
        a = self.wav("a.wav", freq_hz=440, duration_ms=120)
        b = self.wav("b.wav", freq_hz=660, duration_ms=30)
        manifest = write_manifest(
            self.tmp / "dur.json",
            [
                {"id": "a", "source": a.name, "offset_ms": 20, "gain": 1.0},
                {"id": "b", "source": b.name, "offset_ms": 170, "gain": 1.0},
            ],
        )
        out = self.tmp / "out" / "mix.wav"
        self.assertEqual(run_cli(manifest, out).returncode, 0)
        report = load_report(out)
        self.assertEqual(report["total_duration_ms"], 200)
        with wave.open(str(out), "rb") as reader:
            frames = reader.getnframes()
            rate = reader.getframerate()
            # Reported duration and frame count must describe the same span.
            self.assertLessEqual(abs(frames - round(rate * 200 / 1000)), 1)
            self.assertLessEqual(abs(frames * 1000 / rate - 200), 2)

    def test_total_duration_covers_every_clip_end(self):
        # Mixed sample rates are rejected, so check the rounding invariant at
        # a rate where milliseconds are not a whole number of frames.
        a = self.wav("a.wav", freq_hz=440, duration_ms=13, sample_rate=11025)
        b = self.wav("b.wav", freq_hz=660, duration_ms=7, sample_rate=11025)
        manifest = write_manifest(
            self.tmp / "odd.json",
            [
                {"id": "a", "source": a.name, "offset_ms": 20, "gain": 1.0},
                {"id": "b", "source": b.name, "offset_ms": 90, "gain": 1.0},
            ],
        )
        out = self.tmp / "out" / "mix.wav"
        self.assertEqual(run_cli(manifest, out).returncode, 0)
        report = load_report(out)
        total = report["total_duration_ms"]
        max_end = max(c["offset_ms"] + c["duration_ms"] for c in report["clips"])
        self.assertGreaterEqual(total, max_end, "reported duration truncates the last tail")
        self.assertLessEqual(total - max_end, 2, "reported duration overshoots the last tail")
        for clip in report["clips"]:
            self.assertLessEqual(clip["offset_ms"] + clip["duration_ms"], total)
        with wave.open(str(out), "rb") as reader:
            frames = reader.getnframes()
            self.assertLessEqual(abs(frames - round(11025 * total / 1000)), 1)

    def test_fade_shape_matches_manual_calculation(self):
        # 1 kHz sample rate so 5 ms == 5 frames, making the ramp easy to check.
        clip = self.const("tone.wav", value=10000, duration_ms=20, sample_rate=1000)
        manifest = write_manifest(
            self.tmp / "fade.json",
            [{"id": "tone", "source": clip.name, "offset_ms": 0, "gain": 1.0}],
        )
        out = self.tmp / "out" / "mix.wav"
        self.assertEqual(run_cli(manifest, out).returncode, 0)
        samples, channels, rate = read_all_samples(out)
        self.assertEqual((channels, rate), (1, 1000))
        self.assertEqual(len(samples), 20)
        expected = []
        for i in range(20):
            if i < 5:
                factor = i / 5
            elif i >= 15:
                factor = (19 - i) / 5
            else:
                factor = 1.0
            expected.append(round(10000 * factor))
        for actual, want in zip(samples, expected):
            self.assertLessEqual(abs(actual - want), 1, f"sample mismatch: {samples}")

    def test_short_clip_uses_half_length_fades(self):
        # A 6 ms clip has 6 frames at 1 kHz; each fade is 3 frames.
        clip = self.const("short.wav", value=20000, duration_ms=6, sample_rate=1000)
        manifest = write_manifest(
            self.tmp / "short.json",
            [{"id": "short", "source": clip.name, "offset_ms": 0, "gain": 1.0}],
        )
        out = self.tmp / "out" / "mix.wav"
        self.assertEqual(run_cli(manifest, out).returncode, 0)
        samples, _, _ = read_all_samples(out)
        # frame 0: 0, frame 3: (5-3)/3 -> 0.666.., frame 5: 0
        self.assertEqual(samples[0], 0)
        self.assertEqual(samples[5], 0)
        self.assertGreater(samples[2], 0)
        self.assertEqual(samples[2], samples[3])  # both sit at 2/3 of the peak
        self.assertLess(samples[4], samples[3])

    def test_overlap_boundary_matches_manual_calculation(self):
        # 1 kHz: 20 frames per clip, 5-frame fades, 10 ms = 10-frame offset.
        # Clip A: constant 8000, gain 1.0, offset 0.
        # Clip B: constant 5000, gain 1.5, offset 10 (fades over its first 5 frames).
        a = self.const("a.wav", value=8000, duration_ms=20, sample_rate=1000)
        b = self.const("b.wav", value=5000, duration_ms=20, sample_rate=1000)
        manifest = write_manifest(
            self.tmp / "overlap.json",
            [
                {"id": "a", "source": a.name, "offset_ms": 0, "gain": 1.0},
                {"id": "b", "source": b.name, "offset_ms": 10, "gain": 1.5},
            ],
        )
        out = self.tmp / "out" / "mix.wav"
        self.assertEqual(run_cli(manifest, out).returncode, 0)
        samples, _, _ = read_all_samples(out)
        self.assertEqual(len(samples), 30)

        def a_value(i: int) -> float:
            if i < 0 or i >= 20:
                return 0.0
            if i < 5:
                return 8000 * i / 5
            if i >= 15:
                return 8000 * (19 - i) / 5
            return 8000.0

        def b_value(absolute: int) -> float:
            j = absolute - 10
            if j < 0 or j >= 20:
                return 0.0
            if j < 5:
                return 7500.0 * j / 5
            if j >= 15:
                return 7500.0 * (19 - j) / 5
            return 7500.0

        for i in range(30):
            expected = round(a_value(i) + b_value(i))
            self.assertLessEqual(
                abs(samples[i] - expected), 1,
                f"frame {i}: got {samples[i]}, expected {expected}",
            )
        # Spot-check the hand-computed junction frames.
        self.assertEqual(samples[10], 8000)   # B fade starts at 0
        self.assertEqual(samples[14], 14000)  # A sustain 8000 + B 6000
        self.assertEqual(samples[15], 13900)  # A starts fading, B at full

    def test_fade_helpers(self):
        self.assertEqual(fade_frame_count(441, 44100), 220)  # exactly 10 ms -> half each
        self.assertEqual(fade_frame_count(176, 44100), 88)  # 4 ms -> half length
        self.assertEqual(fade_frame_count(44100, 44100), 221)  # 5 ms == 220.5 frames
        self.assertEqual(fade_factor(0, 100, 5), 0.0)
        self.assertEqual(fade_factor(5, 100, 5), 1.0)
        self.assertEqual(fade_factor(99, 100, 5), 0.0)


class TestClipping(MixerTestCase):
    def test_gain_two_clips_clip_is_flagged(self):
        clip = self.const("loud.wav", value=30000, duration_ms=40)
        manifest = write_manifest(
            self.tmp / "gain.json",
            [{"id": "loud", "source": clip.name, "offset_ms": 0, "gain": 2.0}],
        )
        out = self.tmp / "out" / "mix.wav"
        self.assertEqual(run_cli(manifest, out).returncode, 0)
        report = load_report(out)
        clip_entry = report["clips"][0]
        self.assertIs(clip_entry["clipped"], True)
        # A single clip, however loud, cannot clip the *sum*: the per-clip
        # limiter already bounds it to the int16 range.
        self.assertIs(report["overall_clipped"], False)
        samples, _, _ = read_all_samples(out)
        self.assertTrue(all(-32768 <= value <= 32767 for value in samples))
        self.assertIn(max(samples), (32767,))

    def test_overlapping_full_scale_clips_flag_overall_clip(self):
        a = self.const("a.wav", value=32767, duration_ms=40)
        b = self.const("b.wav", value=32767, duration_ms=40)
        manifest = write_manifest(
            self.tmp / "overlap.json",
            [
                {"id": "a", "source": a.name, "offset_ms": 0, "gain": 1.0},
                {"id": "b", "source": b.name, "offset_ms": 5, "gain": 1.0},
            ],
        )
        out = self.tmp / "out" / "mix.wav"
        self.assertEqual(run_cli(manifest, out).returncode, 0)
        report = load_report(out)
        self.assertIs(report["overall_clipped"], True)
        for clip in report["clips"]:
            self.assertIs(clip["clipped"], False)
        samples, _, _ = read_all_samples(out)
        self.assertTrue(all(-32768 <= value <= 32767 for value in samples))
        # The overlap region must sit at the positive rail.
        self.assertEqual(max(samples), 32767)

    def test_no_false_clip_flag_on_quiet_mix(self):
        a = self.const("a.wav", value=1000, duration_ms=30)
        b = self.const("b.wav", value=1000, duration_ms=30)
        manifest = write_manifest(
            self.tmp / "quiet.json",
            [
                {"id": "a", "source": a.name, "offset_ms": 0, "gain": 1.0},
                {"id": "b", "source": b.name, "offset_ms": 10, "gain": 1.0},
            ],
        )
        out = self.tmp / "out" / "mix.wav"
        self.assertEqual(run_cli(manifest, out).returncode, 0)
        report = load_report(out)
        self.assertIs(report["overall_clipped"], False)
        self.assertTrue(all(clip["clipped"] is False for clip in report["clips"]))


class TestOutputAndReport(MixerTestCase):
    def test_output_format_and_report_fields(self):
        a = self.wav("a.wav", freq_hz=440, duration_ms=100, channels=2)
        b = self.wav("b.wav", freq_hz=660, duration_ms=50, channels=2)
        manifest = write_manifest(
            self.tmp / "ok.json",
            [
                {"id": "late", "source": b.name, "offset_ms": 80, "gain": 0.5},
                {"id": "early", "source": a.name, "offset_ms": 0, "gain": 1.0},
            ],
        )
        out = self.tmp / "nested" / "mix.wav"
        self.assertEqual(run_cli(manifest, out).returncode, 0)
        report = load_report(out)

        self.assertEqual(report["status"], "complete")
        self.assertIsInstance(report["total_duration_ms"], int)
        self.assertIsInstance(report["overall_clipped"], bool)
        self.assertEqual(report["format"]["sample_rate"], 44100)
        self.assertEqual(report["format"]["channels"], 2)
        self.assertEqual(report["format"]["bits_per_sample"], 16)
        self.assertEqual([c["offset_ms"] for c in report["clips"]], [0, 80])
        self.assertEqual([c["id"] for c in report["clips"]], ["early", "late"])
        for clip in report["clips"]:
            self.assertIsInstance(clip["clipped"], bool)
            self.assertEqual(clip["status"], "mixed")

        with wave.open(str(out), "rb") as reader:
            self.assertEqual(reader.getnchannels(), 2)
            self.assertEqual(reader.getsampwidth(), 2)
            self.assertEqual(reader.getframerate(), 44100)
            self.assertGreater(reader.getnframes(), 0)

    def test_missing_source_is_excluded_but_mix_completes(self):
        a = self.wav("a.wav", freq_hz=440, duration_ms=60)
        missing = self.wav("gone.wav", freq_hz=660, duration_ms=60)
        manifest = write_manifest(
            self.tmp / "missing.json",
            [
                {"id": "a", "source": a.name, "offset_ms": 0, "gain": 1.0},
                {"id": "gone", "source": missing.name, "offset_ms": 20, "gain": 1.0},
            ],
        )
        missing.unlink()
        out = self.tmp / "out" / "mix.wav"
        result = run_cli(manifest, out)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(out.exists())
        report = load_report(out)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["summary"]["mixed"], 1)
        self.assertEqual(report["summary"]["excluded"], 1)
        self.assertEqual(report["excluded_sources"][0]["source"], "gone.wav")
        self.assertEqual(
            [c["status"] for c in report["clips"]], ["mixed", "excluded"]
        )
        samples, _, _ = read_all_samples(out)
        with wave.open(str(out), "rb") as reader:
            self.assertLessEqual(
                abs(reader.getnframes() - round(44100 * 60 / 1000)), 1
            )
        # The missing clip must not contribute anything: mixing 'a' alone
        # produces exactly the same samples.
        solo_manifest = write_manifest(
            self.tmp / "solo.json",
            [{"id": "a", "source": a.name, "offset_ms": 0, "gain": 1.0}],
        )
        solo_out = self.tmp / "solo" / "mix.wav"
        self.assertEqual(run_cli(solo_manifest, solo_out).returncode, 0)
        solo_samples, _, _ = read_all_samples(solo_out)
        self.assertEqual(samples, solo_samples)

    def test_corrupt_source_is_reported_as_excluded(self):
        a = self.wav("a.wav", freq_hz=440, duration_ms=40)
        corrupt = self.tmp / "corrupt.wav"
        corrupt.write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunk")
        manifest = write_manifest(
            self.tmp / "corrupt.json",
            [
                {"id": "a", "source": a.name, "offset_ms": 0, "gain": 1.0},
                {"id": "bad", "source": corrupt.name, "offset_ms": 10, "gain": 1.0},
            ],
        )
        out = self.tmp / "out" / "mix.wav"
        self.assertEqual(run_cli(manifest, out).returncode, 0)
        report = load_report(out)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["excluded_sources"][0]["source"], "corrupt.wav")
        self.assertEqual(report["excluded_sources"][0]["reason"], "unreadable")
        self.assertTrue(out.exists())

    def test_all_sources_missing_writes_no_wav(self):
        missing_a = self.tmp / "a.wav"
        missing_b = self.tmp / "b.wav"
        manifest = write_manifest(
            self.tmp / "all-missing.json",
            [
                {"id": "a", "source": missing_a.name, "offset_ms": 0, "gain": 1.0},
                {"id": "b", "source": missing_b.name, "offset_ms": 10, "gain": 1.0},
            ],
        )
        out = self.tmp / "out" / "mix.wav"
        result = run_cli(manifest, out)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(out.parent.glob("*.wav")), [])
        report = load_report(out)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["reason"], "no_usable_clips")
        excluded = {item["source"] for item in report["excluded_sources"]}
        self.assertEqual(excluded, {"a.wav", "b.wav"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
