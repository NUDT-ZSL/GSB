from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from poscascade.errors import BusinessError, ErrorCode
from poscascade.store import SnapshotStore

from .helpers import pipeline


class SnapshotStoreTests(unittest.TestCase):
    def test_concurrent_commit_fails_fast_with_stage_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pipeline(root, "busy").run()
            store_path = root / "busy" / "snapshots.sqlite3"
            store = SnapshotStore(store_path)
            generation = store.active_generation()
            self.assertIsNotNone(generation)
            validate_record = store.load_snapshot(generation.generation, "validate")
            blocker = sqlite3.connect(store_path, timeout=0.0, isolation_level=None)
            try:
                blocker.execute("BEGIN IMMEDIATE")
                with self.assertRaises(BusinessError) as raised:
                    store.commit_stage(
                        generation=generation.generation,
                        stage="validate",
                        payload=validate_record.payload,
                        affected_doc_ids=[],
                        recompute_doc_ids=[],
                        reuse_doc_ids=generation.doc_ids,
                    )
                self.assertEqual(raised.exception.code, ErrorCode.E_STAGE_BUSY)
            finally:
                if blocker.in_transaction:
                    blocker.execute("ROLLBACK")
                blocker.close()

    def test_partial_stage_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outcome = pipeline(root, "partial").run()
            self.assertEqual(outcome.code, ErrorCode.OK)
            store = SnapshotStore(root / "partial" / "snapshots.sqlite3")
            generation = store.active_generation()
            record = store.load_snapshot(generation.generation, "validate")
            with self.assertRaises(BusinessError) as raised:
                store.commit_stage(
                    generation=generation.generation,
                    stage="validate",
                    payload=record.payload,
                    affected_doc_ids=[],
                    recompute_doc_ids=[],
                    reuse_doc_ids=generation.doc_ids[:-1],
                )
            self.assertEqual(raised.exception.code, ErrorCode.E_STAGE_INCOMPLETE)

    def test_snapshot_versions_are_globally_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pipeline(root, "monotonic").run()
            store = SnapshotStore(root / "monotonic" / "snapshots.sqlite3")
            versions = [record.snapshot_version for record in store.list_snapshots()]
            self.assertEqual(versions, sorted(versions))
            self.assertEqual(versions, [1, 2, 3, 4])
            for record in store.list_snapshots():
                self.assertEqual(
                    set(record.completed_doc_ids),
                    set(store.active_generation().doc_ids),
                )
