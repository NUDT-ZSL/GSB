from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from layout_order.canonical import atomic_write_json, read_json
from layout_order.errors import LayoutError
from layout_order.snapshot import (
    SnapshotStore,
    build_snapshot,
    make_section,
    replace_snapshot_section,
)


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot = build_snapshot(
            {"page-1": {"page_id": "page-1", "blocks": []}},
            {"body": 1},
            {"weights": {}, "type_violation_penalty": 1.0, "precision": 8},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_whole_snapshot_commit_and_publication(self) -> None:
        store = SnapshotStore(self.root / "snapshots")
        first = store.commit(self.snapshot)
        self.assertTrue(first.changed)
        newer = replace_snapshot_section(
            self.snapshot,
            "block_annotations",
            {"page-1": {"page_id": "page-1", "blocks": [{"block_id": 1}]}},
        )
        second = store.commit(newer)
        self.assertTrue(second.changed)
        self.assertEqual(store.current.snapshot_id, newer.snapshot_id)
        self.assertEqual(
            store.current.section("block_annotations").content,
            newer.section("block_annotations").content,
        )
        # The untouched sections were replaced as a whole snapshot, not patched
        # in place from an external object reference.
        self.assertEqual(
            store.current.section("type_order").content,
            self.snapshot.section("type_order").content,
        )

    def test_rollback_is_rejected(self) -> None:
        store = SnapshotStore(self.root / "snapshots")
        store.commit(self.snapshot)
        newer = replace_snapshot_section(
            self.snapshot,
            "edge_weights",
            {"weights": {"x": 1}, "type_violation_penalty": 1.0, "precision": 8},
        )
        store.commit(newer)
        with self.assertRaises(LayoutError) as context:
            store.commit(self.snapshot)
        self.assertEqual(context.exception.code, "E_SNAPSHOT_ROLLBACK")
        self.assertEqual(store.current.snapshot_id, newer.snapshot_id)

    def test_same_version_different_checksum_is_rollback(self) -> None:
        store = SnapshotStore(self.root / "snapshots")
        store.commit(self.snapshot)
        sections = dict(self.snapshot.sections)
        sections["edge_weights"] = make_section(
            "edge_weights",
            1,
            {"weights": {"changed": 2}, "type_violation_penalty": 1.0, "precision": 8},
        )
        from layout_order.snapshot import LayoutSnapshot

        conflicting = LayoutSnapshot(sections=sections)
        with self.assertRaises(LayoutError) as context:
            store.commit(conflicting)
        self.assertEqual(context.exception.code, "E_SNAPSHOT_ROLLBACK")

    def test_corrupt_checksum_is_rejected(self) -> None:
        store_dir = self.root / "corrupt"
        store = SnapshotStore(store_dir)
        store.commit(self.snapshot)
        payload = read_json(store.current_path)
        payload["sections"]["type_order"]["content"]["body"] = 99
        atomic_write_json(store.current_path, payload)
        with self.assertRaises(LayoutError) as context:
            SnapshotStore(store_dir)
        self.assertEqual(context.exception.code, "E_SNAPSHOT_CORRUPT")

    def test_stage_busy_is_immediate(self) -> None:
        store = SnapshotStore(self.root / "snapshots")
        store.commit(self.snapshot)
        newer = replace_snapshot_section(
            self.snapshot,
            "block_annotations",
            {"page-1": {"page_id": "page-1", "blocks": [{"block_id": 2}]}},
        )
        with store.stage_lease():
            with self.assertRaises(LayoutError) as context:
                store.commit(newer)
            self.assertEqual(context.exception.code, "E_STAGE_BUSY")
        store.commit(newer)
        self.assertEqual(store.current.snapshot_id, newer.snapshot_id)


if __name__ == "__main__":
    unittest.main()

