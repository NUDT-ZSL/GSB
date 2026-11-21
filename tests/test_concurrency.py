from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .helpers import ROOT, cli, load_sample, parse_json, status


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_schedule_same_target_commits_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "base"
            concurrent = Path(temp) / "concurrent"
            load_sample(base)
            load_sample(concurrent)
            cli(base, "schedule", "--clock", "1200", check=True)

            command = [
                sys.executable,
                "-m",
                "batchkernel.cli",
                "--state-dir",
                str(concurrent),
                "schedule",
                "--clock",
                "1200",
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            outputs = [process.communicate(timeout=30) for process in processes]
            for process, (stdout, stderr) in zip(processes, outputs):
                self.assertEqual(
                    process.returncode,
                    0,
                    msg=f"stdout={stdout}\nstderr={stderr}",
                )
                self.assertTrue(parse_json_result(stdout)["ok"])
            self.assertEqual(status(concurrent), status(base))
            verify_result = cli(concurrent, "verify", check=True)
            self.assertTrue(parse_json(verify_result)["ok"])


def parse_json_result(stdout: str) -> dict:
    import json

    return json.loads(stdout)


if __name__ == "__main__":
    unittest.main()

