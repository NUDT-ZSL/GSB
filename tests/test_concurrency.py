from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, run_cli_json


class ConcurrencyTests(unittest.TestCase):
    def test_duplicate_snapshot_is_committed_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            result, _ = run_cli_json(state, "load")
            self.assertEqual(result.returncode, 0)
            command = [
                sys.executable,
                "-m",
                "blockstore",
                "snapshot",
                "--state",
                str(state),
                "--sequence",
                "1",
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=os.environ.copy(),
                )
                for _ in range(3)
            ]
            outputs = [process.communicate(timeout=60) for process in processes]
            for process, (stdout, stderr) in zip(processes, outputs):
                self.assertEqual(process.returncode, 0, stdout + stderr)
            status = run_cli_json(state, "status")[1]
            self.assertIsNotNone(status)
            snapshots = [
                item for item in status["snapshots"] if item["snapshot_id"] == "snap-001"
            ]
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(status["chain_version"], 2)

    def test_duplicate_rollback_request_is_applied_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            result, _ = run_cli_json(state, "load")
            self.assertEqual(result.returncode, 0)
            for sequence in (1, 2, 3):
                result, _ = run_cli_json(
                    state, "snapshot", "--sequence", str(sequence)
                )
                self.assertEqual(result.returncode, 0)
            command = [
                sys.executable,
                "-m",
                "blockstore",
                "rollback",
                "--state",
                str(state),
                "--request-id",
                "rb-001",
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=os.environ.copy(),
                )
                for _ in range(3)
            ]
            responses = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                responses.append(json.loads(stdout))
            self.assertEqual(responses[0], responses[1])
            self.assertEqual(responses[0], responses[2])
            status = run_cli_json(state, "status")[1]
            self.assertIsNotNone(status)
            self.assertEqual(status["current_snapshot_id"], "snap-001")
            rollback_history = status["rollback_history"]
            self.assertEqual(len(rollback_history), 1)
            self.assertEqual(status["chain_version"], 5)
