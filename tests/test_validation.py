from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from audiofp.config import load_task
from audiofp.errors import AudioFPError
from tests.helpers import make_task, write_wav


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.signal_ref = np.sin(
            2 * np.pi * 440.0 * np.arange(16000, dtype=np.float64) / 16000.0
        ) * 0.2
        write_wav(self.root / "ref.wav", self.signal_ref, 16000)
        write_wav(self.root / "query.wav", self.signal_ref[:8000] * 0.5, 16000)
        self.task = make_task(
            self.root / "task.json",
            references=["ref.wav"],
            queries=["query.wav"],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _reload(self) -> None:
        (self.root / "task.json").write_text(
            json.dumps(self.task, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _assert_code(self, code: str) -> None:
        self._reload()
        with self.assertRaises(AudioFPError) as context:
            load_task(self.root / "task.json")
        self.assertEqual(context.exception.code, code)
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "report.json").exists())

    def test_valid_task_does_not_create_state(self) -> None:
        self._reload()
        config = load_task(self.root / "task.json")
        self.assertEqual(len(config.references), 1)
        self.assertFalse((self.root / "state").exists())

    def test_bad_sample_rate(self) -> None:
        write_wav(self.root / "ref.wav", self.signal_ref[:8000], 11025)
        self._assert_code("E_SAMPLE_RATE")

    def test_bad_bit_depth(self) -> None:
        write_wav(self.root / "ref.wav", self.signal_ref[:8000], 16000, sample_width=1)
        self._assert_code("E_BIT_DEPTH")

    def test_bad_channels(self) -> None:
        write_wav(
            self.root / "ref.wav",
            np.column_stack([self.signal_ref, self.signal_ref])[:8000],
            16000,
            channels=2,
        )
        self._assert_code("E_CHANNELS")

    def test_frame_validation(self) -> None:
        self.task["fingerprint"]["hop_length"] = 1024
        self._assert_code("E_FRAME")

    def test_non_increasing_bands(self) -> None:
        self.task["fingerprint"]["band_boundaries_hz"] = [50, 500, 500, 7000]
        self._assert_code("E_BANDS")

    def test_nyquist_validation(self) -> None:
        self.task["fingerprint"]["band_boundaries_hz"] = [50, 500, 9000]
        self._assert_code("E_NYQUIST")

    def test_peak_validation(self) -> None:
        self.task["fingerprint"]["peaks_per_frame"] = 0
        self._assert_code("E_PEAKS")

    def test_score_tolerance_validation(self) -> None:
        self.task["search"]["score_tolerance"] = -0.1
        self._assert_code("E_TOLERANCE")

    def test_offset_tolerance_validation(self) -> None:
        self.task["search"]["time_offset_tolerance_samples"] = float("nan")
        self._assert_code("E_TOLERANCE")

    def test_fixed_scale_validation(self) -> None:
        self.task["search"]["fixed_scale"] = 0
        self._assert_code("E_SCALE")

    def test_concurrency_validation(self) -> None:
        self.task["concurrency_limit"] = 0
        self._assert_code("E_CONCURRENCY")

    def test_memory_validation(self) -> None:
        self.task["memory_budget_bytes"] = 0
        self._assert_code("E_MEMORY")

    def test_cli_error_json_and_no_outputs(self) -> None:
        self.task["fingerprint"]["band_boundaries_hz"] = [50, 700, 600]
        self._reload()
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "audiofp.cli",
                "run",
                "--task",
                str(self.root / "task.json"),
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(process.returncode, 2)
        payload = json.loads(process.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "E_BANDS")
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "report.json").exists())


if __name__ == "__main__":
    unittest.main()

