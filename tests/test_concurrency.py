from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .helpers import ROOT, cli_service, parse_stdout, run_cli


class ConcurrencyTests(unittest.TestCase):
    def test_four_processes_submit_each_window_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            loaded = run_cli(
                "load",
                "--db",
                str(tmp / "db.sqlite"),
                "--lock",
                str(tmp / "db.lock"),
                "--tmp",
                str(tmp / "tmp"),
            )
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT)
            command = [
                sys.executable,
                "-m",
                "schedkernel",
                "dispatch",
                "--db",
                str(tmp / "db.sqlite"),
                "--lock",
                str(tmp / "db.lock"),
                "--tmp",
                str(tmp / "tmp"),
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]
            outputs = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=90)
                self.assertEqual(process.returncode, 0, stderr)
                outputs.append(parse_stdout(
                    subprocess.CompletedProcess(
                        command, process.returncode, stdout, stderr
                    )
                ))
            self.assertEqual(
                sum(item["committed_windows"] for item in outputs),
                10,
            )
            service = cli_service(tmp)
            verify = service.verify()
            self.assertTrue(verify["ok"], verify)
            self.assertEqual(verify["stats"]["committed_windows"], 10)
            self.assertEqual(verify["stats"]["batches"], 14)
            self.assertEqual(verify["stats"]["request_assignments"], 18)


if __name__ == "__main__":
    unittest.main()

