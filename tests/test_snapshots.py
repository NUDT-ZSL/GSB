from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sampling_compat.errors import BusinessError, ErrorCode
from sampling_compat.snapshots import SnapshotRepository, probe_snapshot_file
from tests.helpers import SAMPLES


class SnapshotTests(unittest.TestCase):
    def test_versions_increment_and_whole_snapshot_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshots.json"
            repository = SnapshotRepository(path)
            first = repository.commit_many(
                {
                    "normalized_params": {"req-a": {"temperature": 0.7}},
                    "sampling_decisions": {"req-a": {"token_ids": [1, 2]}},
                }
            )
            self.assertEqual(first["normalized_params"]["version"], 1)
            first_inode = path.stat().st_ino
            second = repository.commit_many(
                {
                    "normalized_params": {"req-a": {"temperature": 0.8}},
                    "sampling_decisions": {"req-a": {"token_ids": [3]}},
                }
            )
            self.assertEqual(second["normalized_params"]["version"], 2)
            self.assertEqual(second["sampling_decisions"]["version"], 2)
            self.assertNotEqual(first_inode, path.stat().st_ino)
            snapshots = repository.load_all()
            self.assertEqual(
                snapshots["normalized_params"]["content"],
                {"req-a": {"temperature": 0.8}},
            )
            self.assertNotIn("old", snapshots["normalized_params"]["content"])

    def test_second_repository_detects_version_change_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshots.json"
            observer = SnapshotRepository(path)
            observer.commit_many({"sampling_decisions": {"v": 1}})
            loaded = observer.load_all()
            self.assertEqual(loaded["sampling_decisions"]["version"], 1)
            writer = SnapshotRepository(path)
            writer.commit_many({"sampling_decisions": {"v": 2}})
            reloaded = observer.load_all()
            self.assertEqual(reloaded["sampling_decisions"]["version"], 2)
            self.assertEqual(reloaded["sampling_decisions"]["content"], {"v": 2})
            self.assertTrue(observer.last_reload["changed"])

    def test_corrupt_checksum_names_entry_and_version(self) -> None:
        path = SAMPLES / "errors" / "snapshots" / "corrupt_checksum.json"
        with self.assertRaises(BusinessError) as raised:
            probe_snapshot_file(path)
        self.assertEqual(raised.exception.code, ErrorCode.SNAPSHOT_CORRUPT)
        self.assertEqual(raised.exception.entry, "sampling_decisions")
        self.assertEqual(raised.exception.version, 7)

    def test_malformed_json_is_snapshot_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(BusinessError) as raised:
                probe_snapshot_file(path)
            self.assertEqual(raised.exception.code, ErrorCode.SNAPSHOT_CORRUPT)
            self.assertEqual(raised.exception.entry, "file")

    def test_bootstrap_snapshot_is_valid(self) -> None:
        snapshots = probe_snapshot_file(SAMPLES / "snapshots" / "bootstrap.json")
        self.assertEqual(
            set(snapshots),
            {
                "vocab_table",
                "normalized_params",
                "sampling_decisions",
                "migration_records",
            },
        )
        self.assertTrue(all(envelope["version"] == 1 for envelope in snapshots.values()))


if __name__ == "__main__":
    unittest.main()
