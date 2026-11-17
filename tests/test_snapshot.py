import os
import tempfile
import unittest

from blocksnap.errors import SnapshotError
from blocksnap.manager import SnapshotManager
from blocksnap.volume import Volume


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.image = os.path.join(self.tmp, "v.bin")
        self.store = os.path.join(self.tmp, "store")
        Volume.create(self.image, 4096, 24)
        self.manager = SnapshotManager(Volume.load(self.image), self.store)

    def tearDown(self):
        self.manager.close()

    def test_create_snapshot_and_version_monotonic(self):
        first = self.manager.create_snapshot("s1")
        self.assertEqual(first.sequence, 1)
        self.assertEqual(self.manager.volume.volume_version, 1)
        self.manager.inject_hex(1, "aa")
        second = self.manager.create_snapshot("s2")
        self.assertEqual(second.sequence, 2)
        self.assertEqual(self.manager.volume.volume_version, 2)
        self.assertNotEqual(first.state_digest, second.state_digest)

    def test_duplicate_name_rejected(self):
        self.manager.create_snapshot("dup")
        with self.assertRaises(SnapshotError) as ctx:
            self.manager.create_snapshot("dup")
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT")

    def test_cow_records_only_changed_blocks(self):
        self.manager.create_snapshot("s1")
        self.manager.inject_hex(3, "deadbeef")
        self.manager.inject_hex(3, "cafe")
        entry = self.manager.manifest.snapshots[-1]
        self.assertEqual(len(entry.blocks), 1)
        self.assertEqual(entry.blocks[0].block_index, 3)

    def test_state_blocks_cover_full_volume(self):
        entry = self.manager.create_snapshot("full")
        self.assertEqual(len(entry.state_blocks), 24)


if __name__ == "__main__":
    unittest.main()
