from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import PYTHON, ROOT, copy_sample, run_cli


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package = copy_sample(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_merge_and_event_sigkill_resume_matches_uninterrupted(self) -> None:
        crashed = self.root / "crashed.sqlite"
        clean = self.root / "clean.sqlite"
        exit_code, payload = run_cli(
            crashed,
            self.package,
            "merge",
            "--clock-step",
            "t0",
            env={"CONFIG_ENGINE_CRASH_AFTER_STEP": "merge:record_migrations"},
        )
        self.assertEqual(exit_code, 97)
        _, mid = run_cli(crashed, self.package, "status", check=True)
        self.assertTrue(mid["recovery_required"])
        _, resumed = run_cli(crashed, self.package, "resume", check=True)
        self.assertEqual(resumed["resumed_operation_count"], 1)

        exit_code, _ = run_cli(
            crashed,
            self.package,
            "reconcile",
            "--clock-step",
            "t3",
            env={"CONFIG_ENGINE_CRASH_AFTER_STEP": "reconcile:event:auto:2"},
        )
        self.assertEqual(exit_code, 97)
        _, mid_events = run_cli(crashed, self.package, "status", check=True)
        self.assertTrue(mid_events["recovery_required"])
        run_cli(crashed, self.package, "resume", check=True)
        _, crashed_status = run_cli(crashed, self.package, "status", check=True)

        run_cli(clean, self.package, "merge", "--clock-step", "t0", check=True)
        run_cli(clean, self.package, "reconcile", "--clock-step", "t3", check=True)
        _, clean_status = run_cli(clean, self.package, "status", check=True)
        self.assertEqual(crashed_status["view"], clean_status["view"])
        self.assertEqual(crashed_status["event_streams"], clean_status["event_streams"])
        self.assertEqual(
            crashed_status["migration_decisions"], clean_status["migration_decisions"]
        )
        _, verify = run_cli(crashed, self.package, "verify", check=True)
        self.assertTrue(verify["ok"])
        actions = [item["action"] for item in crashed_status["recovery_decisions"]]
        self.assertIn("resume_started", actions)
        self.assertIn("resume_completed", actions)


class ConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package = copy_sample(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def spawn(self, db: Path, *arguments: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                PYTHON,
                "-m",
                "config_engine",
                "--db",
                str(db),
                "--package",
                str(self.package),
                *arguments,
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_parallel_merge_commits_once(self) -> None:
        db = self.root / "parallel-merge.sqlite"
        processes = [
            self.spawn(db, "merge", "--clock-step", "t0") for _ in range(4)
        ]
        payloads = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stderr)
            payloads.append(json.loads(stdout))
        versions = {item["view_version"] for item in payloads}
        self.assertEqual(len(versions), 1)
        _, status = run_cli(db, self.package, "status", check=True)
        committed = [
            item for item in status["operations"] if item["state"] == "committed"
        ]
        self.assertEqual(len(committed), 1)
        self.assertEqual(status["view"]["view_version"], next(iter(versions)))

    def test_parallel_sources_match_single_combined_reconcile(self) -> None:
        db = self.root / "parallel-events.sqlite"
        clean = self.root / "clean-events.sqlite"
        run_cli(db, self.package, "merge", "--clock-step", "t0", check=True)
        run_cli(clean, self.package, "merge", "--clock-step", "t0", check=True)
        manual = self.spawn(
            db, "reconcile", "--clock-step", "t3", "--source", "manual"
        )
        auto = self.spawn(db, "reconcile", "--clock-step", "t3", "--source", "auto")
        for process in (manual, auto):
            _, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stderr)
        run_cli(
            clean, self.package, "reconcile", "--clock-step", "t3", check=True
        )
        _, parallel_status = run_cli(db, self.package, "status", check=True)
        _, clean_status = run_cli(clean, self.package, "status", check=True)
        self.assertEqual(parallel_status["view"], clean_status["view"])
        self.assertEqual(
            parallel_status["event_streams"], clean_status["event_streams"]
        )
        _, verify = run_cli(db, self.package, "verify", check=True)
        self.assertTrue(verify["ok"])

    def test_duplicate_event_submission_is_idempotent(self) -> None:
        db = self.root / "idempotent-events.sqlite"
        run_cli(db, self.package, "merge", "--clock-step", "t0", check=True)
        _, first = run_cli(
            db, self.package, "reconcile", "--clock-step", "t3", check=True
        )
        _, second = run_cli(
            db, self.package, "reconcile", "--clock-step", "t3", check=True
        )
        self.assertEqual(first, second)
        _, verify = run_cli(db, self.package, "verify", check=True)
        self.assertTrue(verify["ok"])


if __name__ == "__main__":
    unittest.main()
