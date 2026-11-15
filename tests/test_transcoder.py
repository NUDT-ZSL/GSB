from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from offline_audio.engine import MemoryBudget
from offline_audio.errors import BusinessError

from helpers import (
    REPO_ROOT,
    cli_json,
    read_wav,
    run_cli,
    tool_paths,
    wait_until,
    write_manifest,
    write_wav,
)


def constant(duration: float, sample_rate: int, value: float, channels: int = 1) -> np.ndarray:
    samples = np.full(
        (int(round(duration * sample_rate)), channels), value, dtype=np.float64
    )
    return samples[:, 0] if channels == 1 else samples


def sine(duration: float, sample_rate: int, frequency: float, amplitude: float) -> np.ndarray:
    axis = np.arange(int(round(duration * sample_rate)), dtype=np.float64) / sample_rate
    return amplitude * np.sin(2.0 * np.pi * frequency * axis)


def impulse(duration: float, sample_rate: int, at: float, amplitude: float) -> np.ndarray:
    result = np.zeros(int(round(duration * sample_rate)), dtype=np.float64)
    result[int(round(at * sample_rate))] = amplitude
    return result


def base_manifest(
    root: Path,
    tracks: list[dict[str, object]],
    *,
    sample_rate: int = 48000,
    channels: int = 2,
    bit_depth: int = 16,
    window_ms: float = 100.0,
    peak_policy: str = "clip",
    duration_ms: int | None = None,
) -> dict[str, object]:
    output: dict[str, object] = {
        "file": "mix.wav",
        "sample_rate": sample_rate,
        "channels": channels,
        "channel_layout": ["left", "right"] if channels == 2 else ["mono"],
        "bit_depth": bit_depth,
        "statistics_window_ms": window_ms,
        "max_workers": 3,
        "memory_limit_mb": 64,
        "peak_policy": peak_policy,
    }
    if duration_ms is not None:
        output["duration_ms"] = duration_ms
    return {
        "tracks": tracks,
        "output": output,
        "report_file": "report.json",
        "state_file": "state.json",
        "work_dir": ".work",
    }


class TranscoderTests(unittest.TestCase):
    def setUp(self) -> None:
        tool_paths()
        self.tempdir = tempfile.TemporaryDirectory(prefix="audio-engine-tests-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _manifest(self, manifest: dict[str, object], name: str = "manifest.json") -> Path:
        path = self.root / name
        write_manifest(path, manifest)
        return path

    def test_invalid_offset_returns_business_error_and_no_output(self) -> None:
        write_wav(self.root / "input.wav", constant(0.2, 48000, 0.1), 48000)
        manifest = base_manifest(
            self.root,
            [
                {
                    "file": "input.wav",
                    "sample_rate": 48000,
                    "bit_depth": 16,
                    "offset_ms": -1,
                    "gain": 1.0,
                    "silences": [],
                }
            ],
        )
        result = run_cli(self._manifest(manifest))
        payload = cli_json(result)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "INVALID_MANIFEST")
        self.assertFalse((self.root / "mix.wav").exists())

    def test_declared_bit_depth_mismatch_is_rejected(self) -> None:
        write_wav(self.root / "input.wav", constant(0.2, 48000, 0.1), 48000, bit_depth=24)
        manifest = base_manifest(
            self.root,
            [
                {
                    "file": "input.wav",
                    "sample_rate": 48000,
                    "bit_depth": 16,
                    "offset_ms": 0,
                }
            ],
        )
        result = run_cli(self._manifest(manifest))
        payload = cli_json(result)
        self.assertEqual(payload["error"]["code"], "INPUT_MISMATCH")
        self.assertFalse((self.root / "mix.wav").exists())

    def test_separate_output_spec_file_is_merged(self) -> None:
        write_wav(self.root / "input.wav", constant(0.2, 44100, 0.2), 44100)
        manifest = {
            "tracks": [
                {
                    "file": "input.wav",
                    "sample_rate": 44100,
                    "bit_depth": 16,
                    "offset_ms": 0,
                }
            ]
        }
        spec = {
            "output": {
                "file": "separate-mix.wav",
                "sample_rate": 48000,
                "channels": 1,
                "bit_depth": 16,
                "statistics_window_ms": 100,
                "max_workers": 2,
                "memory_limit_mb": 64,
            },
            "report_file": "separate-report.json",
            "state_file": "separate-state.json",
            "work_dir": ".separate-work",
        }
        path = self._manifest(manifest, "tracks-only.json")
        spec_path = self.root / "output-spec.json"
        write_manifest(spec_path, spec)
        result = run_cli(path, "--output-spec", str(spec_path))
        payload = cli_json(result)
        self.assertTrue(payload["ok"], payload)
        self.assertTrue((self.root / "separate-mix.wav").is_file())
        self.assertTrue((self.root / "separate-report.json").is_file())

    def test_resample_offset_alignment_has_at_most_one_output_sample_error(self) -> None:
        source_rate = 44100
        write_wav(
            self.root / "impulse.wav",
            impulse(0.8, source_rate, 0.25, 0.8),
            source_rate,
        )
        manifest = base_manifest(
            self.root,
            [
                {
                    "file": "impulse.wav",
                    "sample_rate": source_rate,
                    "bit_depth": 16,
                    "offset_ms": 100,
                    "gain": 1.0,
                }
            ],
            sample_rate=48000,
            channels=1,
        )
        result = run_cli(self._manifest(manifest))
        payload = cli_json(result)
        self.assertTrue(payload["ok"], payload)
        samples, sample_rate = read_wav(self.root / "mix.wav")
        self.assertEqual(sample_rate, 48000)
        expected = int(round((100 + 250) * 48000 / 1000))
        actual = int(np.argmax(np.abs(samples[:, 0])))
        self.assertLessEqual(abs(actual - expected), 1, (actual, expected))
        self.assertEqual(expected, 16800)

    def test_channel_mapping_gain_and_order_independent_sum(self) -> None:
        write_wav(self.root / "mono.wav", constant(0.6, 48000, 0.2, 1), 48000)
        stereo = np.zeros((int(0.6 * 48000), 2), dtype=np.float64)
        stereo[:, 0] = 0.3
        stereo[:, 1] = -0.1
        write_wav(self.root / "stereo.wav", stereo, 48000)
        manifest = base_manifest(
            self.root,
            [
                {
                    "file": "mono.wav",
                    "sample_rate": 48000,
                    "bit_depth": 16,
                    "offset_ms": 0,
                    "gain": 0.5,
                },
                {
                    "file": "stereo.wav",
                    "sample_rate": 48000,
                    "bit_depth": 16,
                    "offset_ms": 100,
                    "gain": 1.0,
                },
            ],
            sample_rate=48000,
            channels=2,
        )
        result = run_cli(self._manifest(manifest))
        payload = cli_json(result)
        self.assertTrue(payload["ok"], payload)
        samples, _ = read_wav(self.root / "mix.wav")
        before_offset = samples[1000:4000, :]
        after_offset = samples[6000:10000, :]
        self.assertAlmostEqual(float(before_offset[:, 0].mean()), 0.1, places=3)
        self.assertAlmostEqual(float(before_offset[:, 1].mean()), 0.1, places=3)
        self.assertAlmostEqual(float(after_offset[:, 0].mean()), 0.4, places=3)
        self.assertAlmostEqual(float(after_offset[:, 1].mean()), 0.0, places=3)

    def test_stereo_to_mono_downmix_uses_equal_channel_average(self) -> None:
        stereo = np.zeros((int(0.3 * 44100), 2), dtype=np.float64)
        stereo[:, 0] = 0.2
        stereo[:, 1] = 0.4
        write_wav(self.root / "stereo.wav", stereo, 44100)
        manifest = base_manifest(
            self.root,
            [
                {
                    "file": "stereo.wav",
                    "sample_rate": 44100,
                    "bit_depth": 16,
                    "offset_ms": 0,
                    "gain": 1.0,
                }
            ],
            sample_rate=48000,
            channels=1,
        )
        result = run_cli(self._manifest(manifest))
        payload = cli_json(result)
        self.assertTrue(payload["ok"], payload)
        samples, _ = read_wav(self.root / "mix.wav")
        self.assertAlmostEqual(float(samples[2000:10000, 0].mean()), 0.3, places=3)

    def test_source_time_silence_is_exactly_zero_after_offset(self) -> None:
        write_wav(self.root / "input.wav", constant(0.8, 48000, 0.5), 48000)
        manifest = base_manifest(
            self.root,
            [
                {
                    "file": "input.wav",
                    "sample_rate": 48000,
                    "bit_depth": 16,
                    "offset_ms": 100,
                    "gain": 1.0,
                    "silences": [{"start_ms": 200, "end_ms": 400}],
                }
            ],
            sample_rate=48000,
            channels=1,
        )
        result = run_cli(self._manifest(manifest))
        payload = cli_json(result)
        self.assertTrue(payload["ok"], payload)
        samples, _ = read_wav(self.root / "mix.wav")
        offset = 4800
        silent_start = offset + 9600
        silent_end = offset + 19200
        self.assertTrue(np.all(samples[silent_start:silent_end, 0] == 0.0))
        self.assertGreater(abs(float(samples[silent_start - 1, 0])), 0.49)
        self.assertGreater(abs(float(samples[silent_end, 0])), 0.49)

    def test_source_time_silence_with_resampling_maps_to_target_samples(self) -> None:
        write_wav(self.root / "input.wav", constant(0.8, 44100, 0.5), 44100)
        manifest = base_manifest(
            self.root,
            [
                {
                    "file": "input.wav",
                    "sample_rate": 44100,
                    "bit_depth": 16,
                    "offset_ms": 75,
                    "gain": 1.0,
                    "silences": [{"start_ms": 200.0, "end_ms": 300.0}],
                }
            ],
            sample_rate=48000,
            channels=1,
        )
        result = run_cli(self._manifest(manifest))
        payload = cli_json(result)
        self.assertTrue(payload["ok"], payload)
        samples, _ = read_wav(self.root / "mix.wav")
        silent_start = int(round(75 * 48000 / 1000)) + int(
            round(200.0 * 48000 / 1000)
        )
        silent_end = int(round(75 * 48000 / 1000)) + int(
            round(300.0 * 48000 / 1000)
        )
        self.assertTrue(np.all(samples[silent_start:silent_end, 0] == 0.0))
        self.assertGreater(abs(float(samples[silent_start - 1, 0])), 0.49)
        self.assertGreater(abs(float(samples[silent_end, 0])), 0.49)

    def test_rms_report_window_coordinates_and_silent_windows(self) -> None:
        write_wav(self.root / "short.wav", constant(0.25, 48000, 0.5), 48000)
        manifest = base_manifest(
            self.root,
            [
                {
                    "file": "short.wav",
                    "sample_rate": 48000,
                    "bit_depth": 16,
                    "offset_ms": 0,
                }
            ],
            sample_rate=48000,
            channels=1,
            duration_ms=1000,
            window_ms=100,
        )
        result = run_cli(self._manifest(manifest))
        payload = cli_json(result)
        self.assertTrue(payload["ok"], payload)
        report = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["total_frames"], 48000)
        self.assertEqual(report["window_count"], 10)
        self.assertEqual(report["window_frames"], 4800)
        for index, window in enumerate(report["windows"]):
            self.assertEqual(window["start_sample"], index * 4800)
            self.assertEqual(window["end_sample"], (index + 1) * 4800)
            self.assertAlmostEqual(window["start_ms"], index * 100.0)
            self.assertAlmostEqual(window["end_ms"], (index + 1) * 100.0)
        for window in report["windows"][3:]:
            self.assertEqual(window["rms"], 0.0)
            self.assertEqual(window["peak"], 0.0)

    def test_scale_policy_uses_deterministic_peak_normalisation(self) -> None:
        write_wav(self.root / "a.wav", constant(0.4, 48000, 0.8), 48000)
        write_wav(self.root / "b.wav", constant(0.4, 48000, 0.7), 48000)
        manifest = base_manifest(
            self.root,
            [
                {
                    "file": "a.wav",
                    "sample_rate": 48000,
                    "bit_depth": 16,
                    "offset_ms": 0,
                },
                {
                    "file": "b.wav",
                    "sample_rate": 48000,
                    "bit_depth": 16,
                    "offset_ms": 0,
                },
            ],
            sample_rate=48000,
            channels=1,
            peak_policy="scale",
        )
        result = run_cli(self._manifest(manifest))
        payload = cli_json(result)
        self.assertTrue(payload["ok"], payload)
        samples, _ = read_wav(self.root / "mix.wav")
        report = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        self.assertLessEqual(float(np.max(np.abs(samples))), 1.0)
        self.assertGreater(float(np.max(np.abs(samples))), 0.99)
        self.assertGreater(report["limiter"]["pre_limit_peak"], 1.4)
        self.assertEqual(report["limiter"]["policy"], "scale")
        self.assertAlmostEqual(
            report["limiter"]["applied_scale"],
            1.0 / report["limiter"]["pre_limit_peak"],
            places=6,
        )

    def test_concurrency_level_does_not_change_mix_result(self) -> None:
        for index, value in enumerate((0.1, -0.2, 0.3)):
            write_wav(
                self.root / f"track{index}.wav",
                sine(0.5, 48000, 330 + index * 110, value),
                48000,
            )
        tracks = [
            {
                "file": f"track{index}.wav",
                "sample_rate": 48000,
                "bit_depth": 16,
                "offset_ms": index * 20,
                "gain": 0.9,
            }
            for index in range(3)
        ]
        manifest = base_manifest(self.root, tracks, sample_rate=48000, channels=2)
        path = self._manifest(manifest)
        first = run_cli(
            path,
            "--concurrency",
            "1",
            "--output",
            str(self.root / "serial.wav"),
            "--report",
            str(self.root / "serial.report.json"),
            "--state",
            str(self.root / "serial.state.json"),
            "--work-dir",
            str(self.root / "serial.work"),
        )
        second = run_cli(
            path,
            "--concurrency",
            "4",
            "--output",
            str(self.root / "parallel.wav"),
            "--report",
            str(self.root / "parallel.report.json"),
            "--state",
            str(self.root / "parallel.state.json"),
            "--work-dir",
            str(self.root / "parallel.work"),
        )
        self.assertTrue(cli_json(first)["ok"])
        self.assertTrue(cli_json(second)["ok"])
        serial, _ = read_wav(self.root / "serial.wav")
        parallel, _ = read_wav(self.root / "parallel.wav")
        self.assertEqual(serial.shape, parallel.shape)
        self.assertTrue(np.array_equal(serial, parallel))

    def test_snapshot_change_forces_restart_and_reuses_no_cache(self) -> None:
        write_wav(self.root / "input.wav", constant(0.3, 48000, 0.25), 48000)
        manifest = base_manifest(
            self.root,
            [
                {
                    "file": "input.wav",
                    "sample_rate": 48000,
                    "bit_depth": 16,
                    "offset_ms": 0,
                    "gain": 1.0,
                }
            ],
            sample_rate=48000,
            channels=1,
        )
        path = self._manifest(manifest)
        first = cli_json(run_cli(path))
        self.assertTrue(first["ok"])
        manifest["tracks"][0]["gain"] = 0.5  # type: ignore[index]
        write_manifest(path, manifest)
        second = cli_json(run_cli(path))
        self.assertTrue(second["ok"])
        self.assertEqual(second["recovery"]["mode"], "restart")
        self.assertEqual(second["recovery"]["reason"], "snapshot_changed")
        self.assertEqual(second["recovery"]["reused_tracks"], 0)
        samples, _ = read_wav(self.root / "mix.wav")
        self.assertAlmostEqual(float(samples[:, 0].mean()), 0.125, places=3)

    def test_graceful_sigint_writes_interrupted_state_then_resumes(self) -> None:
        ffmpeg, ffprobe = tool_paths()
        for index in range(3):
            write_wav(
                self.root / f"track{index}.wav",
                sine(0.6, 48000, 300 + index * 100, 0.2),
                48000,
            )
        manifest = base_manifest(
            self.root,
            [
                {
                    "file": f"track{index}.wav",
                    "sample_rate": 48000,
                    "bit_depth": 16,
                    "offset_ms": index * 50,
                }
                for index in range(3)
            ],
        )
        path = self._manifest(manifest)
        state_path = self.root / "state.json"
        command = [
            sys.executable,
            str(REPO_ROOT / "tests" / "interrupt_driver.py"),
            str(path),
            "loading",
            "10",
        ]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            command,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            wait_until(
                lambda: state_path.is_file()
                and json.loads(state_path.read_text(encoding="utf-8"))["stage"] == "loading"
            )
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 130, (stdout, stderr))
        payload = json.loads(stdout.strip().splitlines()[-1])
        self.assertEqual(payload["error"]["code"], "INTERRUPTED")
        self.assertFalse((self.root / "mix.wav").exists())
        interrupted = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(interrupted["stage"], "interrupted")
        resumed = cli_json(run_cli(path))
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(resumed["recovery"]["mode"], "resume")

    def test_hard_kill_after_track_reuses_persisted_track_on_retry(self) -> None:
        for index in range(3):
            write_wav(
                self.root / f"track{index}.wav",
                sine(0.8, 48000, 200 + index * 90, 0.15),
                48000,
            )
        manifest = base_manifest(
            self.root,
            [
                {
                    "file": f"track{index}.wav",
                    "sample_rate": 48000,
                    "bit_depth": 16,
                    "offset_ms": index * 40,
                }
                for index in range(3)
            ],
        )
        path = self._manifest(manifest)
        state_path = self.root / "state.json"
        command = [
            sys.executable,
            str(REPO_ROOT / "tests" / "interrupt_driver.py"),
            str(path),
            "completed",
            "10",
        ]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            command,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            wait_until(
                lambda: state_path.is_file()
                and len(
                    json.loads(
                        state_path.read_text(encoding="utf-8")
                    ).get("completed_tracks", [])
                )
                >= 1
            )
            process.kill()
            process.communicate(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertFalse((self.root / "mix.wav").exists())
        killed = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(killed["completed_tracks"]), 1)
        resumed = cli_json(run_cli(path))
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(resumed["recovery"]["mode"], "resume")
        self.assertGreaterEqual(resumed["recovery"]["reused_tracks"], 1)

    def test_memory_budget_rejects_a_single_oversized_buffer(self) -> None:
        budget = MemoryBudget(64)
        import threading

        with self.assertRaises(BusinessError) as raised:
            budget.acquire(65, threading.Event())
        self.assertEqual(raised.exception.code, "RESOURCE_LIMIT")


if __name__ == "__main__":
    unittest.main()
