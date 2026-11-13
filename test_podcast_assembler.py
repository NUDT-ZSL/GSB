#!/usr/bin/env python3
"""Acceptance tests for podcast_assembler (standard library only).

Run from the project root:

    python -m unittest -v test_podcast_assembler.py

Each test builds its own deterministic WAV inputs in a temporary directory.
"""

from __future__ import annotations

import array
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from typing import Dict, List, Optional

import podcast_assembler as pa

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "podcast_assembler.py")


def make_tone(duration_seconds: float, frequency: float = 440.0) -> array.array:
    frame_count = int(round(duration_seconds * pa.SAMPLE_RATE))
    samples = array.array("h", bytes(frame_count * 2))
    for index in range(frame_count):
        samples[index] = 200 + round(
            250.0 * math.sin(2.0 * math.pi * frequency * index / pa.SAMPLE_RATE)
        )
    return samples


def write_wav(
    path: str,
    samples: array.array,
    channels: int = 1,
    sample_rate: int = pa.SAMPLE_RATE,
    sample_width: int = 2,
) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if channels != 1:
        interleaved = array.array("h", bytes(len(samples) * channels * 2))
        for index, value in enumerate(samples):
            for channel in range(channels):
                interleaved[index * channels + channel] = value
        samples = interleaved
    with wave.open(path, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(samples.tobytes())


def write_manifest(path: str, segments: List[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"segments": segments}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def read_wav(path: str):
    with wave.open(path, "rb") as reader:
        params = (
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getframerate(),
            reader.getnframes(),
        )
        raw = reader.readframes(reader.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return params, samples


class AssemblerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def path(self, name: str) -> str:
        return os.path.join(self.tmp, name)

    def run_cli(
        self, manifest: str, output: Optional[str] = None, report: Optional[str] = None
    ):
        output = output or self.path("out.wav")
        report = report or self.path("report.json")
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                SCRIPT,
                manifest,
                "-o",
                output,
                "-r",
                report,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc, output, report

    def load_report(self, report_path: str) -> dict:
        with open(report_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    # -- criterion 1: manifest validation -------------------------------
    def test_empty_manifest_is_rejected(self) -> None:
        manifest = self.path("empty.json")
        write_manifest(manifest, [])
        proc, output, report = self.run_cli(manifest)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid manifest", proc.stderr)
        self.assertFalse(os.path.exists(output))

    def test_negative_start_is_rejected(self) -> None:
        write_wav(self.path("segments/a.wav"), make_tone(0.5))
        manifest = self.path("negative.json")
        write_manifest(
            manifest, [{"path": "segments/a.wav", "start": -0.5}]
        )
        proc, _, _ = self.run_cli(manifest)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid manifest", proc.stderr)

    def test_missing_path_field_is_rejected(self) -> None:
        manifest = self.path("bad.json")
        write_manifest(manifest, [{"start": 0.0}])
        proc, _, _ = self.run_cli(manifest)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid manifest", proc.stderr)

    # -- criterion 2: sample conversion ----------------------------------
    def test_seconds_to_samples_rounding_and_monotonic(self) -> None:
        self.assertEqual(pa.seconds_to_samples(pa.Decimal("0.001")), 44)
        self.assertEqual(pa.seconds_to_samples(pa.Decimal("0.002")), 88)
        self.assertEqual(pa.seconds_to_samples(pa.Decimal("1.000")), 44100)
        # 0.001 s = 44.1 samples -> 44; 0.0015 s = 66.15 -> 66.
        self.assertEqual(pa.seconds_to_samples(pa.Decimal("0.0015")), 66)
        starts = [pa.Decimal(str(i)) / 1000 for i in range(0, 2001, 10)]
        positions = [pa.seconds_to_samples(value) for value in starts]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(positions, sorted(set(positions)))

    def test_first_valid_sample_offset_matches_conversion(self) -> None:
        source = make_tone(0.4, frequency=523)
        write_wav(self.path("segments/中文片段.wav"), source)
        manifest = self.path("precision.json")
        write_manifest(
            manifest, [{"path": "segments/中文片段.wav", "start": 0.001}]
        )
        proc, output, report = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        params, samples = read_wav(output)
        offset = pa.seconds_to_samples(pa.Decimal("0.001"))
        self.assertEqual(offset, 44)
        self.assertEqual(list(samples[:offset]), [0] * offset)
        self.assertEqual(samples[offset], source[0])
        self.assertEqual(samples[offset:offset + len(source)], source)

    # -- criterion 3: invalid audio format -------------------------------
    def test_stereo_segment_is_skipped(self) -> None:
        write_wav(self.path("segments/good.wav"), make_tone(1.0))
        write_wav(self.path("segments/stereo.wav"), make_tone(1.0), channels=2)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/good.wav", "start": 0.0},
                {"path": "segments/stereo.wav", "start": 1.0},
            ],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(len(report["successful_segments"]), 1)
        issue = report["issues"][0]
        self.assertEqual(issue["path"], "segments/stereo.wav")
        self.assertIn("16-bit PCM mono", issue["description"])
        params, samples = read_wav(output)
        self.assertEqual(params[3] / params[2], 1.0)
        self.assertEqual(params[3], pa.SAMPLE_RATE)

    def test_wrong_sample_rate_is_skipped(self) -> None:
        write_wav(self.path("segments/good.wav"), make_tone(0.5))
        write_wav(
            self.path("segments/wrong_rate.wav"), make_tone(0.5), sample_rate=48000
        )
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/good.wav", "start": 0.0},
                {"path": "segments/wrong_rate.wav", "start": 1.0},
            ],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(len(report["successful_segments"]), 1)
        self.assertEqual(len(report["issues"]), 1)
        _, samples = read_wav(output)
        self.assertEqual(len(samples), pa.SAMPLE_RATE // 2)

    # -- criterion 4: overlap ----------------------------------------------
    def test_overlap_keeps_first_segment(self) -> None:
        first = make_tone(1.0, frequency=440.0)
        second = make_tone(1.0, frequency=880.0)
        write_wav(self.path("segments/first.wav"), first)
        write_wav(self.path("segments/second.wav"), second)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/first.wav", "start": 1.0},
                {"path": "segments/second.wav", "start": 1.5},
            ],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(len(report["successful_segments"]), 1)
        self.assertEqual(report["successful_segments"][0]["path"], "segments/first.wav")
        self.assertEqual(report["issues"][0]["code"], "overlap")
        self.assertIn("overlap", report["issues"][0]["description"].lower())
        params, samples = read_wav(output)
        self.assertEqual(params[3], pa.SAMPLE_RATE * 2)
        # The second clip (880 Hz) must be absent from the whole output.
        start = pa.SAMPLE_RATE
        self.assertEqual(list(samples[start:]), list(first))

    def test_touching_segments_do_not_overlap(self) -> None:
        first = make_tone(1.0, frequency=440.0)
        second = make_tone(1.0, frequency=880.0)
        write_wav(self.path("segments/first.wav"), first)
        write_wav(self.path("segments/second.wav"), second)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/first.wav", "start": 0.0},
                {"path": "segments/second.wav", "start": 1.0},
            ],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(len(report["successful_segments"]), 2)
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["silence_intervals"], [])
        _, samples = read_wav(output)
        self.assertEqual(list(samples[: len(first)]), list(first))
        self.assertEqual(list(samples[len(first):]), list(second))

    # -- criterion 5: missing file ----------------------------------------
    def test_missing_file_is_skipped(self) -> None:
        normal = make_tone(1.0, frequency=523.0)
        write_wav(self.path("segments/normal.wav"), normal)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/absent.wav", "start": 0.0},
                {"path": "segments/normal.wav", "start": 1.0},
            ],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(report["successful_segments"][0]["path"], "segments/normal.wav")
        self.assertEqual(report["issues"][0]["path"], "segments/absent.wav")
        self.assertIn("not found", report["issues"][0]["description"])
        _, samples = read_wav(output)
        self.assertEqual(list(samples[: pa.SAMPLE_RATE]), [0] * pa.SAMPLE_RATE)
        self.assertEqual(
            list(samples[pa.SAMPLE_RATE:]), list(normal)
        )

    # -- criterion 6: fades ------------------------------------------------
    def test_fade_too_long_is_skipped(self) -> None:
        short = make_tone(0.1)
        write_wav(self.path("segments/short.wav"), short)
        good = make_tone(1.0)
        write_wav(self.path("segments/good.wav"), good)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {
                    "path": "segments/short.wav",
                    "start": 0.0,
                    "fade_in_ms": 60,
                    "fade_out_ms": 60,
                },
                {"path": "segments/good.wav", "start": 1.0},
            ],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(len(report["successful_segments"]), 1)
        self.assertEqual(report["issues"][0]["path"], "segments/short.wav")
        self.assertEqual(report["issues"][0]["code"], "fade_too_long")
        self.assertEqual(
            report["issues"][0]["description"].split(":")[0], "fade too long"
        )
        _, samples = read_wav(output)
        self.assertEqual(len(samples), pa.SAMPLE_RATE * 2)

    def test_exact_half_fade_is_allowed(self) -> None:
        samples = make_tone(0.2)
        write_wav(self.path("segments/half.wav"), samples)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [{"path": "segments/half.wav", "start": 0.0, "fade_in_ms": 100}],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(len(report["successful_segments"]), 1)
        self.assertTrue(report["successful_segments"][0]["fade"])
        _, out = read_wav(output)
        fade_samples = pa.milliseconds_to_samples(pa.Decimal(100))
        self.assertEqual(fade_samples * 2, len(samples))
        self.assertEqual(out[0], 0)
        self.assertLess(abs(out[fade_samples - 1]), abs(samples[fade_samples - 1]) + 2)
        self.assertEqual(
            list(out[fade_samples:]), list(samples[fade_samples:])
        )

    def test_fade_values_are_applied_linearly(self) -> None:
        samples = make_tone(0.5, frequency=0.0)  # constant 200
        write_wav(self.path("segments/const.wav"), samples)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {
                    "path": "segments/const.wav",
                    "start": 0.0,
                    "fade_in_ms": 10,
                    "fade_out_ms": 10,
                }
            ],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        entry = report["successful_segments"][0]
        self.assertTrue(entry["fade"])
        self.assertEqual(entry["fade_in_ms"], 10.0)
        self.assertEqual(entry["fade_out_ms"], 10.0)
        _, out = read_wav(output)
        fade = pa.milliseconds_to_samples(pa.Decimal(10))
        middle = list(out[fade: len(out) - fade])
        self.assertEqual(middle, [200] * len(middle))
        self.assertEqual(out[-1], 0)

    # -- criterion 7: output format ---------------------------------------
    def test_output_is_16_bit_mono_44100_with_gaps(self) -> None:
        first = make_tone(1.0)
        second = make_tone(0.5, frequency=880.0)
        write_wav(self.path("segments/first.wav"), first)
        write_wav(self.path("segments/second.wav"), second)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/first.wav", "start": 0.0},
                {"path": "segments/second.wav", "start": 2.0},
            ],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        params, samples = read_wav(output)
        self.assertEqual(params[:3], (1, 2, pa.SAMPLE_RATE))
        # nframes covers the latest end: 2.0 s + 0.5 s = 2.5 s -> 110250.
        self.assertEqual(params[3], pa.SAMPLE_RATE * 2 + len(second))
        self.assertEqual(len(samples), params[3])
        gap = list(samples[len(first): pa.SAMPLE_RATE * 2])
        self.assertEqual(gap, [0] * len(gap))
        self.assertEqual(
            list(samples[pa.SAMPLE_RATE * 2:]),
            list(second),
        )
        report = self.load_report(report_path)
        self.assertEqual(report["duration_seconds"], 2.5)
        self.assertEqual(report["channels"], 1)
        self.assertEqual(report["sample_width_bits"], 16)
        self.assertEqual(report["sample_rate"], 44100)

    # -- criterion 8: report shape ----------------------------------------
    def test_report_has_three_sections_and_disjoint_paths(self) -> None:
        write_wav(self.path("segments/ok.wav"), make_tone(1.0))
        write_wav(self.path("segments/stereo.wav"), make_tone(1.0), channels=2)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/ok.wav", "start": 0.0},
                {"path": "segments/stereo.wav", "start": 1.0},
            ],
        )
        proc, _, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(
            set(report),
            {
                "manifest",
                "output",
                "sample_rate",
                "channels",
                "sample_width_bits",
                "duration_seconds",
                "successful_segments",
                "issues",
                "silence_intervals",
            },
        )
        successful_paths = {item["path"] for item in report["successful_segments"]}
        issue_paths = {item["path"] for item in report["issues"]}
        self.assertFalse(successful_paths & issue_paths)
        entry = report["successful_segments"][0]
        for field in ("path", "start_seconds", "end_seconds", "fade"):
            self.assertIn(field, entry)
        self.assertFalse(entry["fade"])
        for issue in report["issues"]:
            self.assertIn("path", issue)
            self.assertIn("description", issue)

    # -- criterion 9: silence intervals -----------------------------------
    def test_silence_intervals(self) -> None:
        for index, frequency in ((0, 440.0), (2, 660.0), (4, 880.0)):
            write_wav(
                self.path(f"segments/clip{index}.wav"), make_tone(1.0, frequency)
            )
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/clip0.wav", "start": 0.0},
                {"path": "segments/clip2.wav", "start": 2.0},
                {"path": "segments/clip4.wav", "start": 4.0},
            ],
        )
        proc, _, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(
            report["silence_intervals"],
            [
                {"start_seconds": 1.0, "end_seconds": 2.0},
                {"start_seconds": 3.0, "end_seconds": 4.0},
            ],
        )

    def test_silence_before_first_segment(self) -> None:
        write_wav(self.path("segments/one.wav"), make_tone(1.0))
        manifest = self.path("manifest.json")
        write_manifest(manifest, [{"path": "segments/one.wav", "start": 1.0}])
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(
            report["silence_intervals"],
            [{"start_seconds": 0.0, "end_seconds": 1.0}],
        )
        self.assertEqual(report["duration_seconds"], 2.0)
        _, samples = read_wav(output)
        self.assertEqual(samples[0], 0)
        self.assertEqual(len(samples), pa.SAMPLE_RATE * 2)

    # -- criterion 10: ordering -------------------------------------------
    def test_out_of_order_manifest_is_sorted(self) -> None:
        clips = {
            0: (440.0, "a"),
            2: (660.0, "b"),
            4: (880.0, "c"),
        }
        for start, (frequency, name) in clips.items():
            write_wav(
                self.path(f"segments/{name}.wav"), make_tone(1.0, frequency)
            )
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/c.wav", "start": 4.0},
                {"path": "segments/a.wav", "start": 0.0},
                {"path": "segments/b.wav", "start": 2.0},
            ],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        starts = [item["start_seconds"] for item in report["successful_segments"]]
        self.assertEqual(starts, [0.0, 2.0, 4.0])
        self.assertEqual(
            [item["path"] for item in report["successful_segments"]],
            ["segments/a.wav", "segments/b.wav", "segments/c.wav"],
        )
        _, samples = read_wav(output)
        for start, (_, name) in clips.items():
            source = make_tone(1.0, clips[start][0])
            offset = start * pa.SAMPLE_RATE
            self.assertEqual(
                list(samples[offset: offset + len(source)]), list(source)
            )

    # -- criterion 11: exit code with issues --------------------------------
    def test_issues_do_not_change_exit_code(self) -> None:
        write_wav(self.path("segments/ok.wav"), make_tone(1.0))
        write_wav(self.path("segments/stereo.wav"), make_tone(1.0), channels=2)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/ok.wav", "start": 0.0},
                {"path": "segments/stereo.wav", "start": 1.0},
            ],
        )
        proc, _, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0)
        report = self.load_report(report_path)
        self.assertEqual(len(report["issues"]), 1)
        self.assertIn("1 issue", proc.stderr)

    # -- criterion 12: sample fidelity -------------------------------------
    def test_samples_identical_outside_fades(self) -> None:
        first = make_tone(2.0, frequency=440.0)
        second = make_tone(1.0, frequency=880.0)
        write_wav(self.path("segments/first.wav"), first)
        write_wav(self.path("segments/second.wav"), second)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {
                    "path": "segments/first.wav",
                    "start": 0.0,
                    "fade_in_ms": 50,
                    "fade_out_ms": 50,
                },
                {"path": "segments/second.wav", "start": 3.0},
            ],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        _, samples = read_wav(output)
        fade = pa.milliseconds_to_samples(pa.Decimal(50))
        self.assertEqual(samples[0], 0)
        self.assertNotEqual(
            list(samples[:fade]), list(first[:fade]), "fade-in must alter samples"
        )
        self.assertEqual(
            list(samples[fade: len(first) - fade]),
            list(first[fade: len(first) - fade]),
        )
        # Padding around the untouched clip and the second clip.
        self.assertEqual(list(samples[len(first): 3 * pa.SAMPLE_RATE]), [0] * (3 * pa.SAMPLE_RATE - len(first)))
        self.assertEqual(
            list(samples[3 * pa.SAMPLE_RATE:]),
            list(second),
        )

    # -- criterion 13: duplicates -------------------------------------------
    def test_duplicate_path_and_start_kept_once(self) -> None:
        good = make_tone(1.0)
        write_wav(self.path("segments/good.wav"), good)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/good.wav", "start": 0.0},
                {"path": "segments/good.wav", "start": 0.0},
            ],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(len(report["successful_segments"]), 1)
        self.assertEqual(report["issues"][0]["code"], "duplicate")
        self.assertIn("duplicate", report["issues"][0]["description"])
        _, samples = read_wav(output)
        self.assertEqual(len(samples), len(good))
        self.assertEqual(list(samples), list(good))

    def test_same_path_different_start_is_not_duplicate(self) -> None:
        good = make_tone(1.0)
        write_wav(self.path("segments/good.wav"), good)
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/good.wav", "start": 0.0},
                {"path": "segments/good.wav", "start": 2.0},
            ],
        )
        proc, _, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(len(report["successful_segments"]), 2)
        self.assertEqual(report["issues"], [])

    # -- criterion 14: Chinese paths ----------------------------------------
    def test_chinese_paths_are_supported(self) -> None:
        chinese_dir = os.path.join(self.tmp, "片段素材")
        chinese_file = os.path.join(chinese_dir, "开场白.wav")
        write_wav(chinese_file, make_tone(0.4, frequency=523.0))
        manifest = self.path("清单.json")
        write_manifest(
            manifest, [{"path": "片段素材/开场白.wav", "start": 0.001}]
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(
            report["successful_segments"][0]["path"], "片段素材/开场白.wav"
        )
        self.assertIn("开场白", report["successful_segments"][0]["path"])
        with open(report_path, "rb") as handle:
            raw = handle.read()
        raw.decode("utf-8")  # valid UTF-8, no replacement characters
        self.assertNotIn(b"\\u5f00", raw)  # not ASCII-escaped either

    # -- bonus: graceful handling of unwritable outputs ---------------------
    def test_output_write_failure_is_nonzero(self) -> None:
        write_wav(self.path("segments/ok.wav"), make_tone(0.5))
        manifest = self.path("manifest.json")
        write_manifest(manifest, [{"path": "segments/ok.wav", "start": 0.0}])
        # Use a directory as the output path: opening it for writing fails.
        proc, _, _ = self.run_cli(manifest, output=self.tmp)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cannot write output", proc.stderr)

    # -- bonus: edge cases --------------------------------------------------
    def test_all_segments_invalid_still_succeeds(self) -> None:
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/absent1.wav", "start": 0.0},
                {"path": "segments/absent2.wav", "start": 1.0},
            ],
        )
        proc, output, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 issues", proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(report["successful_segments"], [])
        self.assertEqual(report["silence_intervals"], [])
        self.assertEqual(len(report["issues"]), 2)
        params, samples = read_wav(output)
        self.assertEqual(params, (1, 2, pa.SAMPLE_RATE, 0))
        self.assertEqual(len(samples), 0)

    def test_zero_start_is_valid(self) -> None:
        write_wav(self.path("segments/a.wav"), make_tone(0.1))
        manifest = self.path("manifest.json")
        write_manifest(manifest, [{"path": "segments/a.wav", "start": 0}])
        proc, _, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(len(report["successful_segments"]), 1)

    def test_float_start_keeps_decimal_precision(self) -> None:
        # 0.1 s -> exactly 4410 samples; JSON floats are parsed as Decimal.
        write_wav(self.path("segments/a.wav"), make_tone(0.1))
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/a.wav", "start": 0.1},
                {"path": "segments/a.wav", "start": 0.2},
            ],
        )
        proc, _, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(
            [s["start_seconds"] for s in report["successful_segments"]],
            [0.1, 0.2],
        )

    def test_null_fade_is_treated_as_disabled(self) -> None:
        write_wav(self.path("segments/a.wav"), make_tone(0.5))
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {
                    "path": "segments/a.wav",
                    "start": 0.0,
                    "fade_in_ms": None,
                    "fade_out_ms": None,
                }
            ],
        )
        proc, _, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(len(report["successful_segments"]), 1)
        self.assertFalse(report["successful_segments"][0]["fade"])

    def test_boolean_start_is_rejected(self) -> None:
        write_wav(self.path("segments/a.wav"), make_tone(0.1))
        manifest = self.path("manifest.json")
        write_manifest(manifest, [{"path": "segments/a.wav", "start": True}])
        proc, _, _ = self.run_cli(manifest)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid manifest", proc.stderr)

    def test_fade_rounding_is_symmetric_for_negative_samples(self) -> None:
        self.assertEqual(pa._round_half_up(0.5), 1)
        self.assertEqual(pa._round_half_up(-0.5), -1)
        self.assertEqual(pa._round_half_up(-1.5), -2)

    def test_manifest_with_bom_and_crlf_is_read(self) -> None:
        write_wav(self.path("segments/a.wav"), make_tone(0.1))
        manifest = self.path("manifest.json")
        with open(manifest, "w", encoding="utf-8-sig", newline="\r\n") as handle:
            json.dump(
                {"segments": [{"path": "segments/a.wav", "start": 0.0}]},
                handle,
                ensure_ascii=False,
            )
        proc, _, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            len(self.load_report(report_path)["successful_segments"]), 1
        )

    def test_malformed_fade_value_is_a_segment_issue(self) -> None:
        write_wav(self.path("segments/a.wav"), make_tone(0.5))
        write_wav(self.path("segments/b.wav"), make_tone(0.5))
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [
                {"path": "segments/a.wav", "start": 0.0, "fade_in_ms": "abc"},
                {"path": "segments/b.wav", "start": 1.0},
            ],
        )
        proc, _, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(len(report["successful_segments"]), 1)
        self.assertEqual(report["issues"][0]["code"], "invalid_fade")

    def test_one_sided_fade_too_long_is_skipped(self) -> None:
        write_wav(self.path("segments/a.wav"), make_tone(0.1))
        manifest = self.path("manifest.json")
        write_manifest(
            manifest,
            [{"path": "segments/a.wav", "start": 0.0, "fade_out_ms": 60}],
        )
        proc, _, report_path = self.run_cli(manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.load_report(report_path)
        self.assertEqual(report["successful_segments"], [])
        self.assertEqual(report["issues"][0]["code"], "fade_too_long")


if __name__ == "__main__":
    unittest.main(verbosity=2)
