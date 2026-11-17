from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.helpers import base_task, frame_value, write_case


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name != "nt" and hasattr(signal, "SIGTERM"), "POSIX signal test")
class SignalRecoveryTests(unittest.TestCase):
    def test_sigterm_writes_interrupted_state_and_resume_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frames = [frame_value(index, pixels=8) for index in range(80)]
            task = base_task("out")
            task["processing"]["chunk_frames"] = 4
            task["processing"]["concurrency"] = 1
            task_path = write_case(root, task, frames)
            output = root / "out"

            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hdr_color",
                    str(task_path),
                    "--test-chunk-delay-ms",
                    "70",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 10.0
            first_chunk = output / ".hdr-color-work/chunk-000000.json"
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    self.fail(
                        f"process exited before signal: {process.returncode} "
                        f"{stdout!r} {stderr!r}"
                    )
                if first_chunk.exists() and (output / "run-state.json").exists():
                    break
                time.sleep(0.01)
            else:
                process.kill()
                self.fail("timed out waiting for the first persisted chunk")

            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 143, stderr)
            interruption = json.loads(stdout)
            self.assertEqual(interruption["status"], "interrupted")
            self.assertEqual(interruption["signal"], "SIGTERM")
            self.assertEqual(interruption["exit_code"], 143)
            self.assertFalse((output / "converted_frames.json").exists())
            self.assertFalse((output / "statistics_report.json").exists())
            state = json.loads((output / "run-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "interrupted")
            self.assertGreaterEqual(state["completed_chunks"], 1)
            self.assertEqual(list(output.glob("*.tmp")), [])
            self.assertEqual(list((output / ".hdr-color-work").glob("*.tmp")), [])

            resumed = subprocess.run(
                [sys.executable, "-m", "hdr_color", str(task_path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            result = json.loads(resumed.stdout)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["frames_count"], 80)
            report = json.loads(
                (output / "statistics_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["frames_count"], 80)
            self.assertGreaterEqual(
                len(report["processing"]["resumed_from_persisted_chunks"]), 1
            )


if __name__ == "__main__":
    unittest.main()
