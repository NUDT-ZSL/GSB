from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from walcp.engine import AppService

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
NOW = 1_700_000_500_000


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.db"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            loaded = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "walcp",
                    "load",
                    "--db",
                    str(db),
                    "--samples",
                    str(SAMPLES),
                    "--now-ms",
                    str(NOW),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(loaded.returncode, 0, loaded.stdout + loaded.stderr)
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "walcp",
                        "apply",
                        "--db",
                        str(db),
                        "--now-ms",
                        str(NOW),
                    ],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]
            outputs = [process.communicate(timeout=60) for process in processes]
            for process, (stdout, stderr) in zip(processes, outputs):
                self.assertEqual(
                    process.returncode, 0, stdout + stderr
                )
            status = AppService(db).status()
            self.assertEqual(status["watermark"], 22)
            self.assertEqual(status["applied_transactions"], 22)
            self.assertEqual(status["delay_summary"]["count"], 8)
            txn_ids = [
                record["txn_id"] for record in status["delay_summary"]["records"]
            ]
            self.assertEqual(txn_ids, sorted(set(txn_ids)))
            self.assertTrue(AppService(db).verify()["matched"])


if __name__ == "__main__":
    unittest.main()
