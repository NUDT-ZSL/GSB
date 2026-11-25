from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import unittest

from topodiag.engine import build_workspace
from topodiag.sample import generate_samples
from topodiag.storage import runtime_db_path

from .helpers import ROOT, cli_env, make_workdir, status, verify


class ConcurrencyTests(unittest.TestCase):
    def test_two_schedule_processes_do_not_duplicate_instances_or_ledger(self) -> None:
        workdir = make_workdir("topodiag-concurrent-")
        generate_samples(workdir)
        build_workspace(workdir)
        commands = [
            [
                sys.executable,
                "-m",
                "topodiag",
                "schedule",
                "--workdir",
                str(workdir),
                "--round-budget-ms",
                "250",
            ]
            for _ in range(2)
        ]
        processes = [
            subprocess.Popen(
                command,
                cwd=str(ROOT),
                env=cli_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for command in commands
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=90)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        payload = status(workdir)
        self.assertEqual(
            len({item["instance_id"] for item in payload["instances"]}),
            len(payload["instances"]),
        )
        self.assertEqual(len(payload["instances"]), 11)
        self.assertTrue(verify(workdir)["ok"])

    def test_policy_update_can_overlap_scheduling_without_mixed_binding(self) -> None:
        workdir = make_workdir("topodiag-concurrent-policy-")
        generate_samples(workdir)
        build_workspace(workdir)
        schedule = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "topodiag",
                "schedule",
                "--workdir",
                str(workdir),
                "--round-budget-ms",
                "250",
            ],
            cwd=str(ROOT),
            env=cli_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        update = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "topodiag",
                "build",
                "--workdir",
                str(workdir),
                "--policy-package",
                str(workdir / "samples" / "policies" / "v2-update"),
            ],
            cwd=str(ROOT),
            env=cli_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for process in (schedule, update):
            stdout, stderr = process.communicate(timeout=90)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        payload = status(workdir)
        bound_versions = {
            item["policy_version_id"] for item in payload["instances"]
        }
        self.assertEqual(len(bound_versions), 1)
        self.assertTrue(verify(workdir)["ok"])

    def test_two_policy_update_processes_activate_package_once(self) -> None:
        workdir = make_workdir("topodiag-concurrent-update-")
        generate_samples(workdir)
        build_workspace(workdir)
        command = [
            sys.executable,
            "-m",
            "topodiag",
            "build",
            "--workdir",
            str(workdir),
            "--policy-package",
            str(workdir / "samples" / "policies" / "v2-update"),
        ]
        processes = [
            subprocess.Popen(
                command,
                cwd=str(ROOT),
                env=cli_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=90)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        payload = status(workdir)
        self.assertEqual(payload["policy_pointer"]["policy_generation"], 3)
        with sqlite3.connect(str(runtime_db_path(workdir))) as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) FROM policy_versions
                WHERE policy_generation=3 AND state='committed'
                """
            ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertTrue(verify(workdir)["ok"])


if __name__ == "__main__":
    unittest.main()
