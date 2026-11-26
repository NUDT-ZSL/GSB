from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from seqledger.engine import patch_ledger
from seqledger.errors import InputError, ReplayError
from seqledger.sample import generate_fixtures
from seqledger.store import LedgerStore
from seqledger.util import canonical_json, read_json

from .helpers import fixture_paths, prepare_complete_db


class PatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.fixture_dir = Path(cls.tempdir.name) / "fixtures"
        generate_fixtures(cls.fixture_dir)
        cls.paths = fixture_paths(cls.fixture_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    def test_patch_closure_recomputes_only_affected_partitions(self) -> None:
        db = str(Path(self.tempdir.name) / "patch-closure.sqlite3")
        prepare_complete_db(db, self.paths)
        store = LedgerStore(db)
        before, _ = store.read_state()
        before_partitions = {
            pid: canonical_json(partition)
            for pid, partition in before["partitions"].items()
        }
        before_version = before["version"]
        result = patch_ledger(store, read_json(self.paths["patch"]))
        after, _ = store.read_state()
        report = result["invalidation"]
        self.assertFalse(result["idempotent"])
        self.assertEqual(after["version"], before_version + 1)
        self.assertIn("orders/2", report["affected_partitions"])
        self.assertIn("payments/0", report["affected_partitions"])
        self.assertIn("payments/1", report["affected_partitions"])
        affected = set(report["affected_partitions"])
        for pid, partition in after["partitions"].items():
            if pid not in affected:
                self.assertEqual(
                    canonical_json(partition),
                    before_partitions[pid],
                    f"Unchanged partition {pid} was modified by the patch",
                )
        chain_paths = [
            chain["path"] for chain in report["chains"]
        ]
        self.assertIn(
            ["pay-AB-002", "pay-AB-004", "pay-AB-006"],
            chain_paths,
        )
        self.assertGreater(len(report["affected_conclusions"]), 0)
        self.assertGreater(report["reused_conclusion_count"], 0)
        self.assertEqual(
            report["recomputed_partitions"],
            sorted(report["affected_partitions"]),
        )
        self.assertEqual(
            after["semantics"]["v2_current_generation"],
            4,
        )
        self.assertEqual(
            {
                entry["message_key"]: entry
                for entry in after["semantics"]["view"]["entries"]
            }["ord-1004"]["semantic_generation"],
            4,
        )

    def test_duplicate_patch_is_idempotent_and_conflicting_replay_is_rejected(self) -> None:
        db = str(Path(self.tempdir.name) / "patch-idempotent.sqlite3")
        prepare_complete_db(db, self.paths)
        store = LedgerStore(db)
        first = patch_ledger(store, read_json(self.paths["patch"]))
        second = patch_ledger(LedgerStore(db), read_json(self.paths["patch"]))
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["ledger_version"], second["ledger_version"])
        conflicting = read_json(self.paths["patch"])
        conflicting["updates"][0]["delivery_alias"] = "different-content"
        with self.assertRaises(InputError) as caught:
            patch_ledger(LedgerStore(db), conflicting)
        self.assertEqual(caught.exception.code, "PATCH_SCHEMA_INVALID")

    def test_patch_clock_cannot_regress(self) -> None:
        db = str(Path(self.tempdir.name) / "patch-clock.sqlite3")
        prepare_complete_db(db, self.paths)
        patch = read_json(self.paths["patch"])
        patch["logical_clock_ms"] = 6999
        with self.assertRaises(ReplayError) as caught:
            patch_ledger(LedgerStore(db), patch)
        self.assertEqual(caught.exception.code, "LOGICAL_CLOCK_ROLLBACK")


if __name__ == "__main__":
    unittest.main()
