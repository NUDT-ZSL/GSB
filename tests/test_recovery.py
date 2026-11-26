from __future__ import annotations

import json
import signal
import tempfile
import unittest
from pathlib import Path

from seqledger.sample import generate_fixtures
from seqledger.store import LedgerStore

from .helpers import (
    cli,
    fixture_paths,
    load_args,
    merge_args,
    prepare_merged_db,
    replay_args,
    verify_args,
)


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.fixture_dir = Path(cls.tempdir.name) / "fixtures"
        generate_fixtures(cls.fixture_dir)
        cls.paths = fixture_paths(cls.fixture_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    def test_sigkill_during_replay_then_resume_has_no_duplicate_delivery(self) -> None:
        db = str(Path(self.tempdir.name) / "replay-kill.sqlite3")
        prepare_merged_db(db, self.paths)
        killed = cli(*replay_args(db=db, fault_kill_after_commits=5))
        self.assertEqual(killed.returncode, -signal.SIGKILL)
        state, metadata = LedgerStore(db).read_state()
        self.assertGreater(metadata.version, 0)
        for partition in state["partitions"].values():
            self.assertLessEqual(1 if partition["inflight"] else 0, 1)
        resumed = json.loads(
            cli(
                "--db",
                db,
                "resume",
                "--logical-clock-ms",
                "7000",
                check=True,
            ).stdout
        )
        self.assertEqual(resumed["recovered_pointer_version"], metadata.version)
        status = json.loads(cli("--db", db, "status", check=True).stdout)
        self.assertEqual(status["totals"]["backlog_depth"], 0)
        self.assertEqual(status["totals"]["delivered_decision_count"], 18)
        self.assertEqual(status["totals"]["dead_letter_count"], 1)
        state_after, _ = LedgerStore(db).read_state()
        claims = state_after["delivery_claims"]
        self.assertEqual(len(claims), 18)
        self.assertEqual(
            len({claim["event_seq"] for claim in claims.values()}),
            18,
        )
        verified = json.loads(cli(*verify_args(self.paths, db=db), check=True).stdout)
        self.assertTrue(verified["bitwise_match"])

    def test_kill_before_merge_commit_leaves_previous_pointer_intact(self) -> None:
        db = str(Path(self.tempdir.name) / "merge-kill.sqlite3")
        cli(*load_args(self.paths, db=db), check=True)
        before = json.loads(cli("--db", db, "status").stdout)
        killed = cli(
            *merge_args(self.paths, db=db),
            "--fault-kill-before-commit",
        )
        self.assertEqual(killed.returncode, -signal.SIGKILL)
        after = json.loads(cli("--db", db, "status").stdout)
        self.assertEqual(before["ledger_version"], after["ledger_version"])
        self.assertEqual(before["ledger_sha256"], after["ledger_sha256"])
        successful = json.loads(
            cli(*merge_args(self.paths, db=db), check=True).stdout
        )
        self.assertTrue(successful["ok"])
        self.assertFalse(successful["idempotent"])

    def test_ledger_pointer_and_all_versions_are_inspectable(self) -> None:
        db = str(Path(self.tempdir.name) / "inspect.sqlite3")
        prepare_merged_db(db, self.paths)
        inspection = LedgerStore(db).inspect()
        self.assertTrue(inspection["ok"])
        self.assertEqual(
            inspection["versions"][-1]["version"],
            inspection["current_version"],
        )
        self.assertEqual(
            inspection["versions"][-1]["sha256"],
            inspection["current_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
