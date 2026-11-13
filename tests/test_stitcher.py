"""Acceptance tests for wav_stitcher.py.

Run with:  python -m unittest discover -s tests -v

Tests are deterministic and use only the standard library.  They generate
their own 16-bit PCM WAV fixtures with wav_samples.py.
"""

from __future__ import annotations

import io
import json
import math
import re
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import wav_samples  # noqa: E402
import wav_stitcher  # noqa: E402

DB_PATTERN = re.compile(r"^-?(?:\d+\.\d{2}|inf) (?:dBFS|dB)$")


def read_wav(path: Path) -> tuple[wave._wave_params, bytes]:  # type: ignore[attr-defined]
    with wave.open(str(path), "rb") as wav:
        return wav.getparams(), wav.readframes(wav.getnframes())


def samples_of(data: bytes) -> list[int]:
    return wav_stitcher._bytes_to_samples(data).tolist()


def rms_of(samples: list[int]) -> float:
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def dbfs(amplitude: float) -> float:
    return 20.0 * math.log10(amplitude / 32768.0)


class StitcherTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="wav-stitcher-tests-")
        cls.sample_dir = Path(cls._tmp.name) / "samples"
        cls.files = wav_samples.create_sample_set(cls.sample_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def new_workspace(self) -> Path:
        workspace = Path(tempfile.mkdtemp(prefix="case-", dir=self._tmp.name))
        return workspace

    def run_stitch(
        self,
        inputs: list[Path],
        workspace: Path,
        crops: list[tuple[float, float] | None] | None = None,
        target: float | None = -20.0,
    ) -> tuple[int, dict, str]:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code, report = wav_stitcher.stitch(
                [str(p) for p in inputs],
                str(workspace / "out" / "stitched.wav"),
                str(workspace / "report.json"),
                target_loudness=target,
                crops=crops,
            )
        return code, report, stderr.getvalue()

    # -- criterion 1 ------------------------------------------------------ #
    def test_01_sample_rate_mismatch_reported_others_stitched(self) -> None:
        ws = self.new_workspace()
        inputs = [self.files["tone_a"], self.files["sr_22050"], self.files["tone_b"]]
        code, report, _ = self.run_stitch(inputs, ws)

        self.assertEqual(code, 0)
        segments = report["segments"]
        self.assertEqual([s["file_name"] for s in segments],
                         [p.name for p in inputs])
        self.assertEqual(segments[0]["status"], "ok")
        self.assertEqual(segments[1]["status"], "failed")
        self.assertIn("sample rate 22050", segments[1]["reason"])
        self.assertIn("44100", segments[1]["reason"])
        self.assertEqual(segments[2]["status"], "ok")
        self.assertEqual(report["input_count"]["stitched"], 2)
        self.assertTrue((ws / "out" / "stitched.wav").exists())

    def test_01b_eight_bit_and_channel_mismatch_rejected(self) -> None:
        ws = self.new_workspace()
        inputs = [self.files["tone_a"], self.files["eight_bit"],
                  self.files["stereo"], self.files["tone_c"]]
        code, report, _ = self.run_stitch(inputs, ws)
        self.assertEqual(code, 0)
        self.assertIn("8-bit", report["segments"][1]["reason"])
        self.assertIn("channel count 2", report["segments"][2]["reason"])
        self.assertEqual(report["segments"][3]["status"], "ok")

    # -- criterion 2 ------------------------------------------------------ #
    def test_02_crop_window(self) -> None:
        ws = self.new_workspace()
        code, report, _ = self.run_stitch(
            [self.files["tone_a"]], ws, crops=[(0.2, 0.6)]
        )
        self.assertEqual(code, 0)
        crop = report["segments"][0]["crop"]
        self.assertEqual(crop["actual_start"]["value"], 0.2)
        self.assertEqual(crop["actual_end"]["value"], 0.6)
        self.assertEqual(crop["duration"]["value"], 0.4)

        params, data = read_wav(ws / "out" / "stitched.wav")
        self.assertEqual(params.nframes, 17640)
        self.assertAlmostEqual(params.nframes / params.framerate, 0.4, places=6)

    def test_02b_crop_out_of_bounds_is_clamped(self) -> None:
        ws = self.new_workspace()
        crop = (-1.5, 1.6)
        code, report, _ = self.run_stitch(
            [self.files["tone_a"]], ws, crops=[crop]
        )
        self.assertEqual(code, 0)
        detail = report["segments"][0]["crop"]
        self.assertEqual(detail["requested_start"]["value"], -1.5)
        self.assertEqual(detail["actual_start"]["value"], 0.0)
        self.assertEqual(detail["actual_end"]["value"], 1.0)
        self.assertTrue(detail["truncated_to_clip_bounds"])
        params, _ = read_wav(ws / "out" / "stitched.wav")
        self.assertEqual(params.nframes, 44100)

    def test_02c_fractional_crop_quantises_to_frames(self) -> None:
        ws = self.new_workspace()
        # 0.012345 * 44100 = 544.4 frames -> rounds to 544 frames.
        code, report, _ = self.run_stitch(
            [self.files["tone_a"]], ws, crops=[(0.012345, 0.5001)]
        )
        self.assertEqual(code, 0)
        detail = report["segments"][0]["crop"]
        self.assertEqual(detail["actual_start"]["value"], round(544 / 44100, 2))

    # -- criterion 3 ------------------------------------------------------ #
    def test_03_rms_gain_and_peak(self) -> None:
        ws = self.new_workspace()
        code, report, _ = self.run_stitch(
            [self.files["tone_a"]], ws, target=-20.0
        )
        self.assertEqual(code, 0)
        segment = report["segments"][0]
        self.assertAlmostEqual(segment["input_rms"]["value"], -10.0, delta=0.05)
        self.assertAlmostEqual(segment["gain"]["value"], -10.0, delta=0.05)
        self.assertAlmostEqual(segment["final_rms"]["value"], -20.0, delta=0.1)
        self.assertLessEqual(segment["final_peak"]["value"], 0.0)

        _, data = read_wav(ws / "out" / "stitched.wav")
        samples = samples_of(data)
        self.assertLessEqual(dbfs(max(abs(s) for s in samples)), 0.0)
        self.assertAlmostEqual(dbfs(rms_of(samples)), -20.0, delta=0.1)

    def test_03b_gain_clamped_to_20db(self) -> None:
        ws = self.new_workspace()
        code, report, _ = self.run_stitch(
            [self.files["tone_c"]], ws, target=0.0
        )
        self.assertEqual(code, 0)
        gain = report["segments"][0]["gain"]
        self.assertEqual(gain["value"], 20.0)
        self.assertTrue(gain["range_limited"])
        self.assertLessEqual(report["segments"][0]["final_peak"]["value"], 0.0)

    def test_03c_peak_limiting_keeps_output_below_0dbfs(self) -> None:
        ws = self.new_workspace()
        code, report, _ = self.run_stitch(
            [self.files["spikey"]], ws, target=0.0
        )
        self.assertEqual(code, 0)
        gain = report["segments"][0]["gain"]
        self.assertTrue(gain["peak_limited"])
        _, data = read_wav(ws / "out" / "stitched.wav")
        peak = max(abs(s) for s in samples_of(data))
        self.assertLessEqual(peak, 32767)
        self.assertLessEqual(dbfs(peak), 0.0)

    # -- criterion 4 ------------------------------------------------------ #
    def test_04_concatenation_duration_and_format(self) -> None:
        ws = self.new_workspace()
        code, report, _ = self.run_stitch(
            [self.files["tone_a"], self.files["tone_b"]], ws
        )
        self.assertEqual(code, 0)
        params, data = read_wav(ws / "out" / "stitched.wav")
        self.assertEqual(params.nframes, 88200)
        self.assertEqual(params.nchannels, 1)
        self.assertEqual(params.sampwidth, 2)
        self.assertEqual(params.framerate, 44100)
        self.assertAlmostEqual(len(data) / 2 / params.framerate, 2.0, places=6)
        self.assertEqual(report["output"]["frames"], 88200)
        self.assertEqual(report["output"]["duration"]["value"], 2.0)

    def test_04b_stereo_session_preserved(self) -> None:
        ws = self.new_workspace()
        code, report, _ = self.run_stitch([self.files["stereo"]], ws)
        self.assertEqual(code, 0)
        params, _ = read_wav(ws / "out" / "stitched.wav")
        self.assertEqual(params.nchannels, 2)
        self.assertEqual(params.nframes, 44100)

    # -- criterion 5 ------------------------------------------------------ #
    def test_05_report_fields_order_and_format(self) -> None:
        ws = self.new_workspace()
        inputs = [self.files["tone_a"], self.files["sr_22050"], self.files["tone_b"]]
        code, report, _ = self.run_stitch(inputs, ws)
        self.assertEqual(code, 0)
        required = {"file_name", "status", "reason", "crop", "input_rms",
                    "gain", "final_rms", "final_peak", "participated"}
        for segment, path in zip(report["segments"], inputs, strict=True):
            self.assertEqual(segment["file_name"], path.name)
            self.assertTrue(required.issubset(segment), required - set(segment))
            self.assertIn(segment["status"], {"ok", "failed", "skipped"})

        for segment in report["segments"]:
            for key in ("input_rms", "gain", "final_rms", "final_peak"):
                metric = segment[key]
                if metric is None:
                    continue
                self.assertIn("unit", metric)
                self.assertIn("formatted", metric)
                self.assertRegex(metric["formatted"], DB_PATTERN)
                self.assertEqual(round(metric["value"], 2), metric["value"])
                if metric["unit"] == "dBFS":
                    self.assertTrue(metric["formatted"].endswith(" dBFS"))
                else:
                    self.assertTrue(metric["formatted"].endswith(" dB"))

        # The serialised report must also carry 2-decimal values.
        on_disk = json.loads((ws / "report.json").read_text(encoding="utf-8"))
        self.assertEqual([s["file_name"] for s in on_disk["segments"]],
                         [p.name for p in inputs])

    # -- criterion 6 ------------------------------------------------------ #
    def test_06_all_invalid_no_wav_report_lists_reasons(self) -> None:
        ws = self.new_workspace()
        inputs = [self.files["corrupt"], self.files["eight_bit"],
                  self.files["broken_fmt"], self.files["truncated_data"],
                  self.files["truncated_header"]]
        code, report, _ = self.run_stitch(inputs, ws)
        self.assertEqual(code, 1)
        self.assertFalse((ws / "out" / "stitched.wav").exists())
        self.assertFalse((ws / "out").exists())
        self.assertTrue((ws / "report.json").exists())
        self.assertEqual(report["input_count"]["stitched"], 0)
        self.assertEqual(len(report["segments"]), len(inputs))
        for segment in report["segments"]:
            self.assertEqual(segment["status"], "failed")
            self.assertTrue(segment["reason"])
        self.assertFalse(report["output"]["written"])

    # -- criterion 7 ------------------------------------------------------ #
    def test_07_cli_all_arguments(self) -> None:
        ws = self.new_workspace()
        out_wav = ws / "nested" / "dir" / "mix.wav"
        report_path = ws / "nested" / "meta" / "mix.json"
        command = [
            sys.executable, str(ROOT / "wav_stitcher.py"),
            "--inputs", str(self.files["tone_a"]), str(self.files["tone_b"]),
            "--crops", "full", "0.1:0.5",
            "--out-wav", str(out_wav),
            "--report", str(report_path),
            "--target-loudness", "-25",
        ]
        result = subprocess.run(command, capture_output=True, text=True,
                                cwd=str(ROOT), check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(out_wav.exists())
        self.assertTrue(report_path.exists())
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["target_loudness"]["applied"]["value"], -25.0)
        self.assertEqual(report["segments"][1]["crop"]["duration"]["value"], 0.4)
        params, _ = read_wav(out_wav)
        self.assertEqual(params.nframes, 44100 + 17640)

    def test_07b_cli_input_list_file(self) -> None:
        ws = self.new_workspace()
        list_file = ws / "inputs.txt"
        list_file.write_text(
            f"# demo list\n{self.files['tone_a']}\n{self.files['tone_b']}\n",
            encoding="utf-8",
        )
        out_wav = ws / "list.wav"
        report_path = ws / "list.json"
        command = [
            sys.executable, str(ROOT / "wav_stitcher.py"),
            "--input-list", str(list_file),
            "--out-wav", str(out_wav), "--report", str(report_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True,
                                cwd=str(ROOT), check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        params, _ = read_wav(out_wav)
        self.assertEqual(params.nframes, 88200)

    # -- criterion 8 ------------------------------------------------------ #
    def test_08_mixed_valid_and_corrupt(self) -> None:
        ws_valid = self.new_workspace()
        _, valid_report, _ = self.run_stitch(
            [self.files["tone_a"], self.files["tone_b"]], ws_valid
        )
        valid_bytes = (ws_valid / "out" / "stitched.wav").read_bytes()

        ws = self.new_workspace()
        inputs = [self.files["corrupt"], self.files["tone_a"],
                  self.files["truncated_header"], self.files["tone_b"],
                  self.files["broken_fmt"]]
        code, report, _ = self.run_stitch(inputs, ws)
        self.assertEqual(code, 0)
        statuses = [s["status"] for s in report["segments"]]
        self.assertEqual(statuses, ["failed", "ok", "failed", "ok", "failed"])
        self.assertEqual(report["stitched_files"],
                         [self.files["tone_a"].name, self.files["tone_b"].name])
        self.assertEqual(report["excluded_files"][0]["file_name"],
                         self.files["corrupt"].name)
        self.assertEqual(report["excluded_files"][0]["status"], "failed")

        params, _ = read_wav(ws / "out" / "stitched.wav")
        self.assertEqual(params.nframes, 88200)
        # Corrupt clips must not alter the successfully processed audio.
        self.assertEqual((ws / "out" / "stitched.wav").read_bytes(), valid_bytes)
        self.assertTrue(all(s["reason"] for s in report["segments"]
                            if not s["participated"]))

    # -- criterion 9 ------------------------------------------------------ #
    def test_09_zero_length_crop_excluded(self) -> None:
        ws = self.new_workspace()
        inputs = [self.files["tone_long"], self.files["tone_b"]]
        code, report, _ = self.run_stitch(
            inputs, ws, crops=[(2.0, 1.0), None]
        )
        self.assertEqual(code, 0)
        skipped = report["segments"][0]
        self.assertEqual(skipped["status"], "skipped")
        self.assertFalse(skipped["participated"])
        self.assertIn("insufficient duration", skipped["reason"])
        self.assertEqual(skipped["crop"]["duration"]["value"], 0.0)
        self.assertEqual(report["segments"][1]["status"], "ok")
        params, _ = read_wav(ws / "out" / "stitched.wav")
        self.assertEqual(params.nframes, 44100)
        self.assertEqual(report["output"]["frames"], 44100)

    def test_09b_equal_start_and_end_excluded(self) -> None:
        ws = self.new_workspace()
        code, report, _ = self.run_stitch(
            [self.files["tone_a"]], ws, crops=[(0.5, 0.5)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["segments"][0]["status"], "skipped")
        self.assertFalse((ws / "out" / "stitched.wav").exists())

    # -- criterion 10 ----------------------------------------------------- #
    def test_10_out_of_range_target_falls_back_with_warning(self) -> None:
        ws = self.new_workspace()
        code, report, stderr = self.run_stitch(
            [self.files["tone_a"]], ws, target=-100.0
        )
        self.assertEqual(code, 0)
        self.assertIn("WARNING", stderr)
        self.assertIn("-100", stderr)
        target = report["target_loudness"]
        self.assertEqual(target["applied"]["value"], -20.0)
        self.assertTrue(target["fallback_to_default"])
        self.assertEqual(report["segments"][0]["final_rms"]["value"], -20.0)

    def test_10b_cli_warning_goes_to_stderr(self) -> None:
        ws = self.new_workspace()
        command = [
            sys.executable, str(ROOT / "wav_stitcher.py"),
            "--inputs", str(self.files["tone_a"]),
            "--out-wav", str(ws / "a.wav"),
            "--report", str(ws / "a.json"),
            "--target-loudness", "-100",
        ]
        result = subprocess.run(command, capture_output=True, text=True,
                                cwd=str(ROOT), check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING", result.stderr)
        self.assertNotIn("WARNING", result.stdout)
        report = json.loads((ws / "a.json").read_text(encoding="utf-8"))
        self.assertEqual(report["target_loudness"]["applied"]["value"], -20.0)

    def test_10c_target_above_zero_falls_back(self) -> None:
        ws = self.new_workspace()
        code, report, stderr = self.run_stitch(
            [self.files["tone_a"]], ws, target=3.0
        )
        self.assertEqual(code, 0)
        self.assertIn("WARNING", stderr)
        self.assertEqual(report["target_loudness"]["applied"]["value"], -20.0)

    # -- criterion 11 ----------------------------------------------------- #
    def test_11_truncated_wav_fails_without_affecting_others(self) -> None:
        ws = self.new_workspace()
        inputs = [self.files["tone_a"], self.files["truncated_data"],
                  self.files["tone_b"]]
        code, report, _ = self.run_stitch(inputs, ws)
        self.assertEqual(code, 0)
        failed = report["segments"][1]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("truncated", failed["reason"].lower())
        params, _ = read_wav(ws / "out" / "stitched.wav")
        self.assertEqual(params.nframes, 88200)

    def test_11b_missing_file_is_failed_not_fatal(self) -> None:
        ws = self.new_workspace()
        inputs = [self.files["tone_a"], ws / "does_not_exist.wav"]
        code, report, _ = self.run_stitch(inputs, ws)
        self.assertEqual(code, 0)
        self.assertEqual(report["segments"][1]["status"], "failed")
        self.assertIn("not found", report["segments"][1]["reason"])

    # -- CLI validation --------------------------------------------------- #
    def test_cli_rejects_too_many_crops(self) -> None:
        ws = self.new_workspace()
        command = [
            sys.executable, str(ROOT / "wav_stitcher.py"),
            "--inputs", str(self.files["tone_a"]),
            "--crops", "0:1", "0:2",
            "--out-wav", str(ws / "a.wav"),
            "--report", str(ws / "a.json"),
        ]
        result = subprocess.run(command, capture_output=True, text=True,
                                cwd=str(ROOT), check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("crop specifications", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
