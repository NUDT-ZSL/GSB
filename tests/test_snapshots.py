from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from query_understanding.errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    QUError,
)
from query_understanding.pipeline import QueryEngine
from query_understanding.snapshots import SnapshotManager, write_corrupt_snapshot
from query_understanding.util import read_json, with_checksum

from tests.common import DATA


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.taxonomy = read_json(DATA / "taxonomy.json")
        self.dictionary = read_json(DATA / "dictionary.json")
        self.config = read_json(DATA / "config.json")
        self.mappings = read_json(DATA / "mappings.json")
        self.bundle_v1 = {
            "taxonomy": self.taxonomy,
            "dictionary": self.dictionary,
            "config": self.config,
            "mappings": self.mappings,
        }

    def tearDown(self):
        self.temp.cleanup()

    def _bundle_v2(self):
        taxonomy = copy.deepcopy(self.taxonomy)
        taxonomy["version"] = 2
        taxonomy = with_checksum(taxonomy)
        dictionary = copy.deepcopy(self.dictionary)
        dictionary["version"] = 2
        removed = dictionary["entries"].pop()
        dictionary = with_checksum(dictionary)
        config = copy.deepcopy(self.config)
        config["version"] = 2
        config = with_checksum(config)
        mappings = copy.deepcopy(self.mappings)
        mappings["version"] = 2
        mappings = with_checksum(mappings)
        return {
            "taxonomy": taxonomy,
            "dictionary": dictionary,
            "config": config,
            "mappings": mappings,
        }, removed

    def test_whole_replacement_and_rollback(self):
        manager = SnapshotManager(self.root)
        manager.save(1, self.bundle_v1)
        manager.activate(1)
        bundle_v2, removed = self._bundle_v2()
        manager.save(2, bundle_v2)
        manager.activate(2)
        self.assertEqual(manager.current_version, 2)
        self.assertEqual(
            len(manager.active_bundle()["dictionary"]["entries"]),
            len(self.dictionary["entries"]) - 1,
        )
        self.assertNotIn(
            removed["entry_id"],
            {
                row["entry_id"]
                for row in manager.active_bundle()["dictionary"]["entries"]
            },
        )
        reloaded = QueryEngine.from_bundle(manager.active_bundle())
        self.assertEqual(reloaded.taxonomy.version, 2)
        self.assertEqual(reloaded.dictionary.version, 2)
        self.assertEqual(reloaded.mappings.version, 2)
        self.assertEqual(len(reloaded.dictionary.entries), len(self.dictionary["entries"]) - 1)
        with self.assertRaises(QUError) as context:
            manager.activate(1)
        self.assertEqual(context.exception.code, E_SNAPSHOT_ROLLBACK)

    def test_old_content_cannot_overwrite_new_version(self):
        manager = SnapshotManager(self.root)
        manager.save(1, self.bundle_v1)
        manager.activate(1)
        bundle_v2, _ = self._bundle_v2()
        manager.save(2, bundle_v2)
        manager.activate(2)
        changed = copy.deepcopy(self.bundle_v1)
        changed["config"] = copy.deepcopy(self.config)
        changed["config"]["strict_self_check"] = not changed["config"][
            "strict_self_check"
        ]
        changed["config"] = with_checksum(changed["config"])
        with self.assertRaises(QUError) as context:
            manager.save(1, changed)
        self.assertEqual(context.exception.code, E_SNAPSHOT_ROLLBACK)

    def test_corrupt_snapshot_returns_business_code(self):
        manager = SnapshotManager(self.root)
        path = manager.path_for(1)
        write_corrupt_snapshot(path, 1, self.bundle_v1)
        with self.assertRaises(QUError) as context:
            manager.load_file(path, 1)
        self.assertEqual(context.exception.code, E_SNAPSHOT_CORRUPT)
        self.assertEqual(context.exception.category, "snapshot_checksum")

    def test_component_checksum_corruption(self):
        manager = SnapshotManager(self.root)
        broken = copy.deepcopy(self.bundle_v1)
        broken["dictionary"]["checksum"] = "0" * 64
        with self.assertRaises(QUError) as context:
            manager.save(1, broken)
        self.assertEqual(context.exception.code, E_SNAPSHOT_CORRUPT)
        self.assertEqual(context.exception.category, "dictionary_checksum")


if __name__ == "__main__":
    unittest.main()

