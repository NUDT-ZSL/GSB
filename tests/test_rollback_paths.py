import os
import tempfile
import unittest

from blocksnap.manager import SnapshotManager
from blocksnap.rollback import (
    PATH_BITMAP,
    PATH_CHAIN,
    PATH_FULL,
    RollbackEngine,
    rollback,
)
from blocksnap.volume import Volume


class RollbackPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.image = os.path.join(self.tmp, "v.bin")
        self.store = os.path.join(self.tmp, "store")
        Volume.create(self.image, 4096, 32)
        self.manager = SnapshotManager(Volume.load(self.image), self.store)
        self.manager.create_snapshot("s0")
        for index in (3, 9):
            self.manager.inject_hex(index, "deadbeef")
        self.manager.create_snapshot("s1")
        for index in (5, 9, 20):
            self.manager.inject_hex(index, "01020304")
        self.manager.create_snapshot("s2")

    def tearDown(self):
        self.manager.close()

    def _digest_for_path(self, target, path):
        engine = RollbackEngine(self.manager)
        tmp = os.path.join(self.tmp, f"{path}.tmp")
        if path == PATH_BITMAP:
            engine.build_bitmap_temp(target, tmp)
        elif path == PATH_FULL:
            engine.build_full_temp(target, tmp)
        else:
            engine.build_chain_temp(target, tmp)
        from blocksnap.digest import sha256_file
        return sha256_file(tmp)

    def test_three_paths_bit_identical_each_target(self):
        for target in self.manager.list_snapshots():
            digests = {
                path: self._digest_for_path(target, path)
                for path in (PATH_BITMAP, PATH_FULL, PATH_CHAIN)
            }
            self.assertEqual(len(set(digests.values())), 1, digests)
            self.assertEqual(digests[PATH_CHAIN], target.state_digest)

    def test_auto_path_selection_threshold(self):
        engine = RollbackEngine(self.manager)
        first = self.manager.get_snapshot("s0")
        self.assertEqual(engine.choose_path(first, 0.25), PATH_CHAIN)
        middle = self.manager.get_snapshot("s1")
        self.assertEqual(engine.choose_path(middle, 0.25), PATH_CHAIN)
        newest = self.manager.get_snapshot("s2")
        self.assertEqual(engine.choose_path(newest, 0.25), PATH_CHAIN)

    def test_rollback_report_fields_and_swap(self):
        report = rollback(self.manager, "s1", path=PATH_CHAIN)
        self.assertTrue(report["bit_identical"])
        self.assertIsInstance(report["peak_memory_estimate"], int)
        self.assertEqual(report["post_rollback_digest"],
                         self.manager.get_snapshot("s1").state_digest)
        self.assertTrue(self.manager.detached)


    def test_single_snapshot_threshold_selection(self):
        import os
        import tempfile
        from blocksnap.volume import Volume as V
        from blocksnap.manager import SnapshotManager as SM
        tmp = tempfile.mkdtemp()
        image = os.path.join(tmp, "v.bin")
        store = os.path.join(tmp, "store")
        V.create(image, 4096, 16)
        manager = SM(V.load(image), store)
        manager.create_snapshot("only")
        for index in range(8):  # 50% > 0.25
            manager.inject_hex(index, "c0ffee")
        engine = RollbackEngine(manager)
        target = manager.get_snapshot("only")
        self.assertEqual(engine.choose_path(target, 0.25), PATH_FULL)
        manager.close()


if __name__ == "__main__":
    unittest.main()