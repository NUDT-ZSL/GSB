from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ProcessPoolTests(unittest.TestCase):
    def test_spawn_process_pool_demo_is_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + environment.get(
                "PYTHONPATH", ""
            )
            command = [
                sys.executable,
                "-m",
                "igeval.demo",
                "--samples",
                "4",
                "--workers",
                "2",
                "--output",
                str(root / "report.json"),
                "--summary",
                str(root / "summary.txt"),
                "--runtime-dir",
                str(root / "runtime"),
            ]
            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["initial_batch"]["status"], "ok")
            self.assertEqual(report["final_self_check"]["status"], "ok")
            self.assertTrue(report["verification"]["process_pool_used"])
            self.assertTrue(report["verification"]["query_scores_match_batch"])
            self.assertGreater(len(report["batch_worker_pids"]), 1)


if __name__ == "__main__":
    unittest.main()

