from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, cli, cli_json, load_args, sample_paths


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = sample_paths(self.root / "data")
        self.db = self.root / "state" / "ledger.db"
        self.pointer = self.root / "state" / "pointer.json"
        self.reports = self.root / "state" / "reports"
        loaded = cli_json(
            *load_args(self.paths),
            db=self.db,
            pointer=self.pointer,
            reports=self.reports,
        )
        self.assertEqual(loaded["version"], 0)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _query(self, sql: str) -> list[tuple]:
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def test_crash_after_commit_recovers_without_reapplying(self) -> None:
        crashed = cli(
            "lease",
            "--round",
            "1",
            db=self.db,
            pointer=self.pointer,
            reports=self.reports,
            env={"LEASE_ARBITER_TEST_CRASH_AFTER_COMMIT": "1"},
            check=False,
        )
        self.assertEqual(crashed.returncode, 137)
        self.assertEqual(
            self._query("SELECT value FROM meta WHERE key='ledger_version'")[0][0],
            "1",
        )
        self.assertEqual(json.loads(self.pointer.read_text())["version"], 0)
        lease_count_before = self._query("SELECT COUNT(*) FROM leases")[0][0]

        resumed = cli_json(
            "resume",
            "--round",
            "1",
            db=self.db,
            pointer=self.pointer,
            reports=self.reports,
        )
        self.assertTrue(
            {"rewrote_pointer", "restored_missing_report"}
            & set(resumed["recovery"]["actions"])
        )
        self.assertEqual(json.loads(self.pointer.read_text())["version"], 1)
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM leases")[0][0], lease_count_before
        )
        self.assertEqual(self._query("SELECT COUNT(*) FROM rounds")[0][0], 1)
        verify = cli_json(
            "verify", db=self.db, pointer=self.pointer, reports=self.reports
        )
        self.assertTrue(verify["ok"], verify)

    def test_crash_before_commit_replays_only_uncommitted_round(self) -> None:
        crashed = cli(
            "lease",
            "--round",
            "1",
            db=self.db,
            pointer=self.pointer,
            reports=self.reports,
            env={"LEASE_ARBITER_TEST_CRASH_BEFORE_COMMIT": "1"},
            check=False,
        )
        self.assertEqual(crashed.returncode, 137)
        self.assertEqual(self._query("SELECT COUNT(*) FROM rounds")[0][0], 0)
        self.assertEqual(self._query("SELECT COUNT(*) FROM leases")[0][0], 0)
        self.assertEqual(json.loads(self.pointer.read_text())["version"], 0)

        resumed = cli_json(
            "resume",
            "--round",
            "1",
            db=self.db,
            pointer=self.pointer,
            reports=self.reports,
        )
        self.assertEqual(resumed["report"]["ledger_version"], 1)
        self.assertGreater(self._query("SELECT COUNT(*) FROM leases")[0][0], 0)
        self.assertEqual(self._query("SELECT COUNT(*) FROM rounds")[0][0], 1)
        verify = cli_json(
            "verify", db=self.db, pointer=self.pointer, reports=self.reports
        )
        self.assertTrue(verify["ok"], verify)

    def test_concurrent_same_round_commits_exactly_once(self) -> None:
        command = [
            __import__("sys").executable,
            "-m",
            "lease_arbiter",
            "--db",
            str(self.db),
            "--pointer",
            str(self.pointer),
            "--reports",
            str(self.reports),
            "lease",
            "--round",
            "1",
        ]
        processes = [
            subprocess.Popen(
                command,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(5)
        ]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout))
        self.assertEqual(
            {item["ledger_version"] for item in outputs},
            {1},
        )
        self.assertEqual(
            {item["report_digest"] for item in outputs},
            {outputs[0]["report_digest"]},
        )
        self.assertEqual(self._query("SELECT COUNT(*) FROM rounds")[0][0], 1)
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM leases")[0][0],
            len(outputs[0]["grants"]),
        )
        verify = cli_json(
            "verify", db=self.db, pointer=self.pointer, reports=self.reports
        )
        self.assertTrue(verify["ok"], verify)


if __name__ == "__main__":
    unittest.main()
