from __future__ import annotations

import json
import subprocess
import sys
import unittest

from .support import ROOT, SCENARIO, fresh_dir, run_cli, wait_signature


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        clean_dir = fresh_dir("recovery-clean")
        completed = run_cli(
            "run",
            "--sample",
            str(SCENARIO),
            "--workdir",
            str(clean_dir),
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr or completed.stdout)
        cls.clean = json.loads(completed.stdout)

    def _crash_and_resume(self, phase: str, tick: int) -> dict:
        workdir = fresh_dir(f"recovery-{phase}")
        crashed = run_cli(
            "run",
            "--sample",
            str(SCENARIO),
            "--workdir",
            str(workdir),
            "--crash-at",
            phase,
            "--crash-tick",
            str(tick),
        )
        self.assertEqual(
            crashed.returncode,
            137,
            f"expected forced termination, stdout={crashed.stdout},"
            f" stderr={crashed.stderr}",
        )
        before = run_cli("status", "--workdir", str(workdir))
        self.assertEqual(before.returncode, 0, before.stderr)
        before_status = json.loads(before.stdout)
        self.assertIn(before_status["run"]["status"], {"RUNNING", "COMPLETE"})
        resumed = run_cli("resume", "--workdir", str(workdir))
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        resumed_status = json.loads(resumed.stdout)
        self.assertEqual(
            resumed_status["run"]["ledger_digest"],
            self.clean["run"]["ledger_digest"],
        )
        self.assertEqual(
            wait_signature(resumed_status),
            wait_signature(self.clean),
        )
        verify = run_cli("verify", "--workdir", str(workdir))
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertTrue(json.loads(verify.stdout)["ok"])
        return resumed_status

    def test_crash_during_enqueue(self) -> None:
        self._crash_and_resume("enqueue", 1)

    def test_crash_during_grant(self) -> None:
        self._crash_and_resume("grant", 0)

    def test_crash_during_abandon(self) -> None:
        self._crash_and_resume("abandon", 3)

    def test_two_concurrent_resume_instances_are_idempotent(self) -> None:
        workdir = fresh_dir("recovery-concurrent")
        crashed = run_cli(
            "run",
            "--sample",
            str(SCENARIO),
            "--workdir",
            str(workdir),
            "--crash-at",
            "enqueue",
            "--crash-tick",
            "1",
        )
        self.assertEqual(crashed.returncode, 137)
        command = [
            sys.executable,
            "-m",
            "lockledger",
            "--workspace",
            str(ROOT),
            "resume",
            "--workdir",
            str(workdir),
        ]
        first = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        second = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        first_out, first_err = first.communicate(timeout=30)
        second_out, second_err = second.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, first_err or first_out)
        self.assertEqual(second.returncode, 0, second_err or second_out)
        final = run_cli("status", "--workdir", str(workdir))
        final_status = json.loads(final.stdout)
        self.assertEqual(
            final_status["run"]["ledger_digest"],
            self.clean["run"]["ledger_digest"],
        )
        self.assertEqual(wait_signature(final_status), wait_signature(self.clean))
        verify = run_cli("verify", "--workdir", str(workdir))
        self.assertEqual(verify.returncode, 0, verify.stderr)


if __name__ == "__main__":
    unittest.main()

