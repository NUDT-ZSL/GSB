from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from seqledger.sample import generate_fixtures
from seqledger.store import LedgerStore

from .helpers import (
    cli,
    fixture_paths,
    prepare_complete_db,
    verify_args,
)


class VerifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.fixture_dir = Path(cls.tempdir.name) / "fixtures"
        generate_fixtures(cls.fixture_dir)
        cls.paths = fixture_paths(cls.fixture_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    def test_verify_is_bitwise_and_includes_event_checks(self) -> None:
        db = str(Path(self.tempdir.name) / "verify-ok.sqlite3")
        prepare_complete_db(db, self.paths)
        result = json.loads(cli(*verify_args(self.paths, db=db), check=True).stdout)
        self.assertTrue(result["bitwise_match"])
        self.assertEqual(result["conclusion"], "serial replay and persisted ledger are bitwise-identical")
        self.assertEqual(len(result["event_checks"]), 18)
        self.assertTrue(all(check["match"] for check in result["event_checks"]))
        self.assertEqual(len(result["partition_checks"]), 7)

    def test_verify_detects_semantically_corrupted_active_snapshot(self) -> None:
        db = str(Path(self.tempdir.name) / "verify-tamper.sqlite3")
        prepare_complete_db(db, self.paths)
        store = LedgerStore(db)

        def corrupt(state: dict) -> dict:
            state["partitions"]["orders/0"]["delivery_generation"] += 10
            return state

        store.commit(corrupt)
        result = cli(*verify_args(self.paths, db=db))
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 4)
        self.assertEqual(payload["error"]["code"], "VERIFY_MISMATCH")


if __name__ == "__main__":
    unittest.main()
