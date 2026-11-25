from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import LEDGER, ROOT, cli


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_schedulers_commit_once_and_match_single_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "shared.db"
            self.assertEqual(
                cli(
                    "load",
                    str(ROOT / "samples" / "default"),
                    "--db",
                    str(db_path),
                ).returncode,
                0,
            )
            processes = [
                subprocess.Popen(
                    [sys.executable, str(LEDGER), "run", "--db", str(db_path)],
                    cwd=str(ROOT),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]
            outputs = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, stderr)
                outputs.append(json.loads(stdout))
            committed_runs = [
                output
                for output in outputs
                if output["newly_committed_windows"] > 0
            ]
            self.assertEqual(len(committed_runs), 1)
            self.assertEqual(
                sum(output["newly_committed_windows"] for output in outputs),
                12,
            )
            self.assertEqual(
                sum(output["newly_committed_batches"] for output in outputs),
                15,
            )
            verified = cli("verify", "--db", str(db_path))
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            self.assertTrue(json.loads(verified.stdout)["bitwise_match"])

    def test_repeated_run_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "idempotent.db"
            self.assertEqual(
                cli(
                    "load",
                    str(ROOT / "samples" / "default"),
                    "--db",
                    str(db_path),
                ).returncode,
                0,
            )
            first = cli("run", "--db", str(db_path))
            self.assertEqual(first.returncode, 0, first.stderr)
            before = cli("status", "--db", str(db_path)).stdout
            second = cli("run", "--db", str(db_path))
            self.assertEqual(second.returncode, 0, second.stderr)
            payload = json.loads(second.stdout)
            self.assertEqual(payload["newly_committed_windows"], 0)
            self.assertEqual(payload["newly_committed_batches"], 0)
            self.assertEqual(payload["newly_executed_batches"], 0)
            after = cli("status", "--db", str(db_path)).stdout
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
