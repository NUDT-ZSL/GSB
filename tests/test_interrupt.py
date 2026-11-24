from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from .helpers import ROOT, write_scene


class InterruptTests(unittest.TestCase):
    def _start_delayed(self, scene_path: Path):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        environment["GSB_TEST_TILE_DELAY"] = "0.35"
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "volumetric_engine",
                "render",
                "--scene",
                str(scene_path),
                "--no-stability",
                "--no-trace",
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_sigint_at_tile_boundary_then_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scene_path = write_scene(
                root / "case",
                width=16,
                height=12,
                step_size=0.25,
                max_steps=80,
                tile={"width": 4, "height": 3},
                max_workers=2,
            )
            process = self._start_delayed(scene_path)
            time.sleep(0.45)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 130, (stdout, stderr))
            payload = json.loads(stdout)
            self.assertEqual(payload["code"], "E_INTERRUPTED")
            self.assertTrue(Path(payload["interrupt_marker"]).exists())
            self.assertFalse((root / "output" / "grayscale.png").exists())

            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT)
            resumed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "volumetric_engine",
                    "render",
                    "--scene",
                    str(scene_path),
                    "--no-stability",
                    "--no-trace",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(resumed.returncode, 0, (resumed.stdout, resumed.stderr))
            resumed_payload = json.loads(resumed.stdout)
            self.assertTrue(resumed_payload["ok"])
            self.assertTrue((root / "output" / "grayscale.png").exists())
            self.assertFalse((root / "state" / "interrupted.json").exists())

    def test_sigterm_exit_code_143(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scene_path = write_scene(
                root / "case",
                width=16,
                height=12,
                step_size=0.25,
                max_steps=80,
                tile={"width": 4, "height": 3},
                max_workers=2,
            )
            process = self._start_delayed(scene_path)
            time.sleep(0.45)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 143, (stdout, stderr))
            payload = json.loads(stdout)
            self.assertEqual(payload["code"], "E_INTERRUPTED")
            self.assertEqual(payload["signal"], "SIGTERM")
            marker = Path(payload["interrupt_marker"])
            self.assertTrue(marker.exists())
            marker_payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(marker_payload["exit_code"], 143)
            self.assertFalse((root / "output" / "grayscale.png").exists())


if __name__ == "__main__":
    unittest.main()

