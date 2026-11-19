from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .helpers import (
    NOW,
    ROOT,
    initial_source_args,
    json_stdout,
    pipeline_args,
    run_cli,
)


class RecoveryAndCliTests(unittest.TestCase):
    def test_invalid_manifest_does_not_create_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "bad.json"
            manifest.write_text('{"format":"cache-v1","format":"duplicate"}\n', encoding="utf-8")
            state = root / "state"
            completed = run_cli(
                state,
                "merge",
                "--v1",
                str(manifest),
                "--now-ms",
                NOW,
            )
            self.assertEqual(completed.returncode, 2)
            error = json_stdout(completed)
            self.assertEqual(error["error"]["code"], "CACHE_MANIFEST_INVALID")
            self.assertFalse(state.exists())

    def test_staged_operation_is_recovered_without_rejudging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            interrupted = run_cli(
                state,
                "merge",
                *initial_source_args(),
                "--fault-point",
                "after_stage",
            )
            self.assertEqual(interrupted.returncode, 75)
            before = json_stdout(run_cli(state, "status"))
            self.assertFalse(before["has_snapshot"])
            self.assertEqual(before["pending_operation_count"], 1)
            resumed = run_cli(state, "resume")
            self.assertEqual(resumed.returncode, 0)
            resume_report = json_stdout(resumed)
            self.assertEqual(resume_report["pending_after"], 0)
            self.assertEqual(resume_report["index_version"], 1)
            after = json_stdout(run_cli(state, "status"))
            self.assertEqual(after["index_version"], 1)
            self.assertEqual(after["artifact_count"], 11)
            self.assertEqual(after["pending_operation_count"], 0)

    def test_two_concurrent_merges_commit_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            argv = [
                sys.executable,
                "-m",
                "cacheman",
                "--state-dir",
                str(state),
                "merge",
                *initial_source_args(),
            ]
            first = subprocess.Popen(
                argv,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            second = subprocess.Popen(
                argv,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            first_stdout, first_stderr = first.communicate(timeout=30)
            second_stdout, second_stderr = second.communicate(timeout=30)
            self.assertEqual(first.returncode, 0, first_stderr)
            self.assertEqual(second.returncode, 0, second_stderr)
            statuses = sorted(
                [json.loads(first_stdout)["status"], json.loads(second_stdout)["status"]]
            )
            self.assertEqual(statuses, ["committed", "noop"])
            report = json_stdout(run_cli(state, "status"))
            self.assertEqual(report["index_version"], 1)
            self.assertEqual(report["pending_operation_count"], 0)

    def test_clock_rollback_is_a_business_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            merged = run_cli(state, "merge", *initial_source_args())
            self.assertEqual(merged.returncode, 0)
            judged = run_cli(
                state,
                "judge",
                "--pipeline",
                "samples/pipelines/app-ci.json",
                "--now-ms",
                "1",
            )
            self.assertEqual(judged.returncode, 2)
            self.assertEqual(
                json_stdout(judged)["error"]["code"],
                "CACHE_CLOCK_ROLLBACK",
            )

    def test_demo_runs_twice_byte_identically(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/run_demo.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        summary = json.loads(completed.stdout)
        self.assertTrue(summary["byte_identical"])
        first = (ROOT / "demo_output/run1.json").read_bytes()
        second = (ROOT / "demo_output/run2.json").read_bytes()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
