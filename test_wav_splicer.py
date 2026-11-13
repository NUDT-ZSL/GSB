#!/usr/bin/env python3
"""End-to-end verification for wav_splicer.py using deterministic local samples.

Run with:  python -m unittest -v test_wav_splicer.py
The tests build their own 16-bit mono PCM WAV fixtures (8 kHz and 48 kHz),
drive the command line exactly as a producer would, and compare every output
sample against the source slices.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

SPLICER = Path(__file__).with_name("wav_splicer.py")


# ---------------------------------------------------------------------------
# Deterministic fixture helpers
# ---------------------------------------------------------------------------


def make_samples(frame_count: int, seed: int = 0) -> list[int]:
    """Deterministic, non-silent 16-bit samples with lots of value variety."""
    return [((index * 7919 + seed * 104729) % 65536) - 32768 for index in range(frame_count)]


def pack_mono(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def write_mono_wav(path: Path, samples: list[int], sample_rate: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pack_mono(samples))


def write_stereo_wav(path: Path, samples: list[int], sample_rate: int) -> None:
    interleaved = []
    for value in samples:
        interleaved.extend((value, value))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(struct.pack(f"<{len(interleaved)}h", *interleaved))


def read_output(path: Path) -> tuple[wave._wave_params, bytes]:
    with wave.open(str(path), "rb") as handle:
        params = handle.getparams()
        frames = handle.readframes(handle.getnframes())
    return params, frames


def data_chunk_offset(raw: bytes) -> int:
    pos = 12
    while pos + 8 <= len(raw):
        chunk_id, chunk_size = struct.unpack_from("<4sI", raw, pos)
        if chunk_id == b"data":
            return pos
        pos += 8 + chunk_size + (chunk_size & 1)
    raise AssertionError("fixture has no data chunk")


class SplicerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wav-splicer-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def write_plan(self, segments: object, name: str = "plan.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
        return path

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SPLICER), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.tmp),
        )

    def run_job(
        self,
        input_wav: Path,
        plan: Path,
        output: Path,
        report: Path,
        *extra: str,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            "--input",
            str(input_wav),
            "--plan",
            str(plan),
            "--output",
            str(output),
            "--report",
            str(report),
            *extra,
        )

    def load_report(self, report: Path) -> dict:
        return json.loads(report.read_text(encoding="utf-8"))

    # ==================================================================
    # 1 + 7 + 8 -- valid three segments, exact samples, complete report
    # ==================================================================
    def test_valid_three_segments_concatenates_exactly_in_plan_order(self) -> None:
        rate = 8000
        samples = make_samples(rate)  # exactly 1000 ms
        source = self.tmp / "source.wav"
        write_mono_wav(source, samples, rate)
        source_bytes = pack_mono(samples)

        segments = [
            {"start_ms": 0, "end_ms": 100},
            {"start_ms": 120, "end_ms": 250},
            {"start_ms": 300, "end_ms": 450},
        ]
        plan = self.write_plan(segments)
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"

        result = self.run_job(source, plan, output, report)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output.is_file())

        params, frames = read_output(output)
        self.assertEqual(params.nchannels, 1)
        self.assertEqual(params.sampwidth, 2)
        self.assertEqual(params.framerate, rate)

        # 100 + 130 + 150 = 380 ms -> 3040 frames at 8 kHz
        expected_frames = 3040
        self.assertEqual(params.nframes, expected_frames)
        self.assertEqual(len(frames), expected_frames * 2)

        # Sample-for-sample equality against the source ranges, in plan order.
        expected = b"".join(
            source_bytes[seg["start_ms"] * rate * 2 // 1000 : seg["end_ms"] * rate * 2 // 1000]
            for seg in segments
        )
        self.assertEqual(frames, expected)

        # Also assert that the spliced order differs from a naive whole-source
        # prefix, i.e. segment order really follows the plan.
        self.assertEqual(frames[0:1600], source_bytes[0:1600])
        self.assertEqual(frames[1600:3680], source_bytes[1920:4000])
        self.assertEqual(frames[3680:], source_bytes[4800:7200])

        data = self.load_report(report)
        self.assertEqual(data["total_segments"], 3)
        self.assertIs(data["valid"], True)
        self.assertEqual(data["errors"], [])

    # ==================================================================
    # 1 (order) -- output follows plan order even when not chronological
    # ==================================================================
    def test_plan_order_is_respected_for_non_chronological_segments(self) -> None:
        rate = 8000
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(rate), rate)  # 1000 ms
        source_bytes = pack_mono(make_samples(rate))

        segments = [
            {"start_ms": 700, "end_ms": 800},  # later audio first
            {"start_ms": 0, "end_ms": 100},
            {"start_ms": 400, "end_ms": 450},
        ]
        plan = self.write_plan(segments)
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"
        result = self.run_job(source, plan, output, report)
        self.assertEqual(result.returncode, 0, result.stderr)

        params, frames = read_output(output)
        self.assertEqual(params.nframes, 800 + 800 + 400)
        self.assertEqual(
            frames,
            b"".join(
                source_bytes[s["start_ms"] * 16 : s["end_ms"] * 16] for s in segments
            ),
        )

    # ==================================================================
    # 2 -- out of range / start >= end: index + reason, no output WAV
    # ==================================================================
    def test_invalid_ranges_are_reported_per_segment_and_no_wav_written(self) -> None:
        rate = 8000
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(rate * 2), rate)  # 2000 ms
        segments = [
            {"start_ms": 0, "end_ms": 100},
            {"start_ms": 2500, "end_ms": 2600},  # start and end beyond duration
            {"start_ms": 500, "end_ms": 500},     # start == end
        ]
        plan = self.write_plan(segments)
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"

        result = self.run_job(source, plan, output, report)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists(), "no production WAV may be created")

        data = self.load_report(report)
        self.assertIs(data["valid"], False)
        self.assertEqual(data["total_segments"], 3)
        self.assertEqual(
            [item["segment_index"] for item in data["errors"]], [1, 2]
        )
        self.assertIn("2000", data["errors"][0]["reason"])
        self.assertIn("start_ms", data["errors"][1]["reason"])
        self.assertIn("less than end_ms", data["errors"][1]["reason"])
        for item in data["errors"]:
            self.assertTrue(item["reason"].strip())

    # ==================================================================
    # 3 -- overlapping segments are cut independently and concatenated
    # ==================================================================
    def test_overlapping_segments_preserve_samples_and_durations(self) -> None:
        rate = 8000
        samples = make_samples(2 * rate)  # 2000 ms
        source = self.tmp / "source.wav"
        write_mono_wav(source, samples, rate)
        source_bytes = pack_mono(samples)

        segments = [
            {"start_ms": 600, "end_ms": 1100},  # overlaps the next segment
            {"start_ms": 800, "end_ms": 1300},
        ]
        plan = self.write_plan(segments)
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"

        result = self.run_job(source, plan, output, report)
        self.assertEqual(result.returncode, 0, result.stderr)

        params, frames = read_output(output)
        self.assertEqual(params.nframes, (500 + 500) * rate // 1000)  # 8000 frames
        first = source_bytes[600 * rate // 1000 * 2 : 1100 * rate // 1000 * 2]
        second = source_bytes[800 * rate // 1000 * 2 : 1300 * rate // 1000 * 2]
        self.assertEqual(len(first), 4000 * 2)
        self.assertEqual(len(second), 4000 * 2)
        # Overlap does not deduplicate or alter anything: both slices appear.
        self.assertEqual(frames, first + second)
        # Source frames 6400..8800 (ms 800..1100) appear in both slices.
        # Segment 1 holds them at output frames 1600..4000; segment 2 at
        # output frames 4000..6400.
        self.assertEqual(
            frames[1600 * 2 : 4000 * 2],
            frames[4000 * 2 : 6400 * 2],
            "overlapping source region is copied into both segments",
        )
        self.assertEqual(self.load_report(report)["valid"], True)

    # ==================================================================
    # 4 -- format / sample-rate gating
    # ==================================================================
    def test_stereo_input_is_rejected(self) -> None:
        source = self.tmp / "stereo.wav"
        write_stereo_wav(source, make_samples(8000), 8000)
        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"

        result = self.run_job(source, plan, output, report)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        data = self.load_report(report)
        self.assertIs(data["valid"], False)
        reasons = " ".join(item["reason"] for item in data["errors"])
        self.assertIn("channel", reasons)
        self.assertIn("stereo", reasons)

    def test_sample_rate_below_minimum_is_rejected(self) -> None:
        source = self.tmp / "rate7999.wav"
        write_mono_wav(source, make_samples(7999), 7999)
        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"

        result = self.run_job(source, plan, output, report)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        reasons = " ".join(item["reason"] for item in self.load_report(report)["errors"])
        self.assertIn("7999", reasons)
        self.assertIn("sample rate", reasons)

    def test_non_16_bit_input_is_rejected(self) -> None:
        source = self.tmp / "8bit.wav"
        with wave.open(str(source), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(1)
            handle.setframerate(8000)
            handle.writeframes(bytes((index % 251) for index in range(800)))
        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"

        result = self.run_job(source, plan, output, report)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        reasons = " ".join(item["reason"] for item in self.load_report(report)["errors"])
        self.assertIn("bits_per_sample", reasons)
        self.assertIn("16-bit", reasons)

    def test_boundary_sample_rates_8000_and_48000_are_accepted(self) -> None:
        for rate in (8000, 48000):
            with self.subTest(rate=rate):
                frames_per_ms = rate // 1000
                samples = make_samples(frames_per_ms * 600, seed=rate)
                source = self.tmp / f"source-{rate}.wav"
                write_mono_wav(source, samples, rate)
                plan = self.write_plan(
                    [{"start_ms": 0, "end_ms": 50}, {"start_ms": 60, "end_ms": 120}],
                    name=f"plan-{rate}.json",
                )
                output = self.tmp / f"episode-{rate}.wav"
                report = self.tmp / f"report-{rate}.json"

                result = self.run_job(source, plan, output, report)
                self.assertEqual(result.returncode, 0, result.stderr)
                params, frames = read_output(output)
                self.assertEqual(params.framerate, rate)
                self.assertEqual(params.nframes, (50 + 60) * frames_per_ms)
                self.assertIs(self.load_report(report)["valid"], True)

    # ==================================================================
    # 5 -- truncated / inconsistent data chunk is corruption
    # ==================================================================
    def test_declared_length_larger_than_actual_data_is_reported(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        raw = bytearray(source.read_bytes())
        offset = data_chunk_offset(raw)
        declared = struct.unpack_from("<I", raw, offset + 4)[0]
        struct.pack_into("<I", raw, offset + 4, declared + 100)  # lie about data size
        struct.pack_into("<I", raw, 4, struct.unpack_from("<I", raw, 4)[0] + 100)
        corrupt = self.tmp / "declared-too-long.wav"
        corrupt.write_bytes(raw)

        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"
        result = self.run_job(corrupt, plan, output, report)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        data = self.load_report(report)
        self.assertIs(data["valid"], False)
        reasons = " ".join(item["reason"] for item in data["errors"])
        self.assertIn("truncated", reasons)

    def test_physically_truncated_file_is_reported(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        raw = source.read_bytes()
        corrupt = self.tmp / "truncated.wav"
        corrupt.write_bytes(raw[:-100])  # chop 50 samples off the tail
        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"

        result = self.run_job(corrupt, plan, output, report)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        data = self.load_report(report)
        self.assertIs(data["valid"], False)
        reasons = " ".join(item["reason"] for item in data["errors"])
        self.assertIn("truncated", reasons)

    def test_non_aligned_data_chunk_is_reported_as_corrupt(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        raw = bytearray(source.read_bytes())
        offset = data_chunk_offset(raw)
        struct.pack_into("<I", raw, offset + 4, 1)  # one byte of a 2-byte frame
        corrupt = self.tmp / "odd-data.wav"
        corrupt.write_bytes(raw)
        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"

        result = self.run_job(corrupt, plan, output, report)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        reasons = " ".join(item["reason"] for item in self.load_report(report)["errors"])
        self.assertIn("data chunk size", reasons)

    # ==================================================================
    # 6 -- existing output: refuse by default, replace only with --force
    # ==================================================================
    def test_existing_output_requires_explicit_force(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        plan = self.write_plan([{"start_ms": 0, "end_ms": 200}])
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"
        output.write_bytes(b"OLD-PRODUCTION-DO-NOT-TOUCH")

        refused = self.run_job(source, plan, output, report)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(output.read_bytes(), b"OLD-PRODUCTION-DO-NOT-TOUCH")
        data = self.load_report(report)
        self.assertIs(data["valid"], False)
        reasons = " ".join(item["reason"] for item in data["errors"])
        self.assertIn("already exists", reasons)
        self.assertIn("--force", reasons)

        replaced = self.run_job(source, plan, output, report, "--force")
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertNotEqual(output.read_bytes(), b"OLD-PRODUCTION-DO-NOT-TOUCH")
        params, _ = read_output(output)
        self.assertEqual(params.nframes, 1600)
        self.assertIs(self.load_report(report)["valid"], True)

    # ==================================================================
    # 8 -- report schema (explicit, standalone)
    # ==================================================================
    def test_success_report_schema(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        plan = self.write_plan([{"start_ms": 0, "end_ms": 50}, {"start_ms": 200, "end_ms": 300}])
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"
        result = self.run_job(source, plan, output, report)
        self.assertEqual(result.returncode, 0, result.stderr)

        data = self.load_report(report)
        self.assertEqual(set(data), {"total_segments", "valid", "errors"})
        self.assertEqual(data["total_segments"], 2)
        self.assertIs(data["valid"], True)
        self.assertIsInstance(data["errors"], list)
        self.assertEqual(data["errors"], [])

    # ==================================================================
    # 9 -- CLI surface and missing required argument behavior
    # ==================================================================
    def test_help_lists_all_five_options(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for option in ("--input", "--plan", "--output", "--report", "--force"):
            self.assertIn(option, result.stdout)
        self.assertIn("usage", result.stdout.lower())

    def test_missing_output_writes_neither_report_nor_wav(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"

        result = self.run_cli(
            "--input", str(source), "--plan", str(plan), "--report", str(report)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--output", result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    def test_missing_input_writes_nothing(self) -> None:
        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"
        result = self.run_cli(
            "--plan", str(plan), "--output", str(output), "--report", str(report)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--input", result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    # ==================================================================
    # 10 -- failure paths never leave partial output / never clobber
    # ==================================================================
    def test_corrupt_input_with_force_leaves_old_production_untouched(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        raw = source.read_bytes()
        corrupt = self.tmp / "corrupt.wav"
        corrupt.write_bytes(raw[:-200])
        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        output = self.tmp / "episode.wav"
        output.write_bytes(b"OLD-PRODUCTION")
        report = self.tmp / "report.json"

        result = self.run_job(corrupt, plan, output, report, "--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), b"OLD-PRODUCTION")
        self.assertFalse(any(self.tmp.glob(".*.tmp")), "no temporary leftovers")
        self.assertIs(self.load_report(report)["valid"], False)

    def test_unwritable_output_location_leaves_no_partial_wav(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        output = self.tmp / "missing-dir" / "episode.wav"
        report = self.tmp / "report.json"
        result = self.run_job(source, plan, output, report, "--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        self.assertFalse(output.parent.exists())
        data = self.load_report(report)
        self.assertIs(data["valid"], False)
        self.assertFalse(any(self.tmp.glob(".*.tmp")), "no temporary leftovers")

    def test_output_parent_is_a_file_fails_without_partial_wav(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        not_a_dir = self.tmp / "blocker"
        not_a_dir.write_bytes(b"i am a file")
        output = not_a_dir / "episode.wav"
        report = self.tmp / "report.json"
        result = self.run_job(source, plan, output, report, "--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        self.assertEqual(not_a_dir.read_bytes(), b"i am a file")
        self.assertIs(self.load_report(report)["valid"], False)
        self.assertFalse(any(self.tmp.glob(".*.tmp")), "no temporary leftovers")

    def test_read_only_existing_output_is_not_replaced_even_with_force(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        output = self.tmp / "episode.wav"
        output.write_bytes(b"LOCKED-OLD-PRODUCTION")
        try:
            output.chmod(0o444)
        except OSError as exc:  # pragma: no cover - platform dependent
            self.skipTest(f"cannot mark file read-only: {exc}")
        self.addCleanup(lambda: output.chmod(0o666))
        report = self.tmp / "report.json"

        result = self.run_job(source, plan, output, report, "--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), b"LOCKED-OLD-PRODUCTION")
        data = self.load_report(report)
        self.assertIs(data["valid"], False)
        self.assertFalse(any(self.tmp.glob(".*.tmp")), "no temporary leftovers")

    def test_path_conflicts_are_rejected_before_writing(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        original = source.read_bytes()
        plan = self.write_plan([{"start_ms": 0, "end_ms": 100}])
        report = self.tmp / "report.json"

        result = self.run_cli(
            "--input", str(source),
            "--plan", str(plan),
            "--output", str(source),
            "--report", str(report),
            "--force",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(report.exists())

    # ==================================================================
    # 11 -- all bad segments listed, ascending segment_index
    # ==================================================================
    def test_multiple_invalid_segments_are_all_listed_in_ascending_order(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)  # 1000 ms
        segments = [
            {"start_ms": 0, "end_ms": 100},      # 0 valid
            {"start_ms": 400, "end_ms": 400},    # 1 start >= end
            {"start_ms": 150, "end_ms": 260},    # 2 valid
            {"start_ms": 1500, "end_ms": 1600},  # 3 beyond duration
        ]
        plan = self.write_plan(segments)
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"

        result = self.run_job(source, plan, output, report)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        data = self.load_report(report)
        self.assertEqual([item["segment_index"] for item in data["errors"]], [1, 3])
        self.assertIn("less than end_ms", data["errors"][0]["reason"])
        self.assertIn("1500", data["errors"][1]["reason"])
        self.assertIn("1000.000", data["errors"][1]["reason"])

    # ==================================================================
    # Plan-shape extras
    # ==================================================================
    def test_malformed_plan_entry_is_reported_with_index(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        plan = self.write_plan(
            [
                {"start_ms": "0", "end_ms": 100},
                {"start_ms": 200},
                "not-an-object",
            ]
        )
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"

        result = self.run_job(source, plan, output, report)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        data = self.load_report(report)
        self.assertEqual([item["segment_index"] for item in data["errors"]], [0, 1, 2])
        self.assertIn("start_ms must be an integer", data["errors"][0]["reason"])
        self.assertIn("end_ms is missing", data["errors"][1]["reason"])

    def test_plan_segments_is_empty_yields_valid_empty_wav(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        plan = self.write_plan({"segments": []})
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"
        result = self.run_job(source, plan, output, report)
        self.assertEqual(result.returncode, 0, result.stderr)
        params, frames = read_output(output)
        self.assertEqual(params.nframes, 0)
        self.assertEqual(frames, b"")
        data = self.load_report(report)
        self.assertEqual(data["total_segments"], 0)
        self.assertIs(data["valid"], True)
        self.assertEqual(data["errors"], [])

    def test_invalid_json_plan_is_reported_without_output(self) -> None:
        source = self.tmp / "source.wav"
        write_mono_wav(source, make_samples(8000), 8000)
        plan = self.tmp / "broken.json"
        plan.write_text("[{\"start_ms\": 0,", encoding="utf-8")
        output = self.tmp / "episode.wav"
        report = self.tmp / "report.json"
        result = self.run_job(source, plan, output, report)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        data = self.load_report(report)
        self.assertIs(data["valid"], False)
        self.assertEqual(len(data["errors"]), 1)
        self.assertEqual(data["errors"][0]["segment_index"], -1)
        self.assertIn("valid JSON", data["errors"][0]["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
