from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliProcessTests(unittest.TestCase):
    def _args(self, root: Path, name: str) -> list[str]:
        return [
            "--state-dir",
            str(root / name / "state"),
            "--cache-dir",
            str(root / name / "cache"),
            "--temp-dir",
            str(root / name / "tmp"),
            "--artifacts-dir",
            str(root / name / "artifacts"),
        ]

    def test_real_subprocess_hard_kill_then_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            full = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "quantlab.cli",
                    "run",
                    "--clean",
                    *self._args(root, "full"),
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(full.returncode, 0, full.stderr)
            full_summary = json.loads(full.stdout.strip().splitlines()[-1])
            crashed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "quantlab.cli",
                    "crash-run",
                    "--clean",
                    "--crash-after-layer",
                    "0",
                    *self._args(root, "recovery"),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(crashed.returncode, 23, crashed.stderr)
            crash_info = json.loads(crashed.stdout.strip().splitlines()[-1])
            self.assertTrue(crash_info["forced_termination"])
            self.assertEqual(crash_info["completed_layers"], ["layer_00"])
            resumed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "quantlab.cli",
                    "resume",
                    *self._args(root, "recovery"),
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            resumed_summary = json.loads(resumed.stdout.strip().splitlines()[-1])
            self.assertEqual(
                full_summary["prediction_checksum"],
                resumed_summary["prediction_checksum"],
            )
            self.assertEqual(
                resumed_summary["reused_inference_layers"],
                ["layer_00"],
            )
            self.assertEqual(
                resumed_summary["recomputed_inference_layers"],
                ["layer_01", "layer_02", "layer_03"],
            )
            self.assertTrue(resumed_summary["consistency"])


if __name__ == "__main__":
    unittest.main()
