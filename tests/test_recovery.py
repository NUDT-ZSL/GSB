import os
import tempfile
import unittest

from blocksnap.manager import SnapshotManager
from blocksnap.rollback import PATH_CHAIN, rollback
from blocksnap.volume import Volume


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.image = os.path.join(self.tmp, "v.bin")
        self.store = os.path.join(self.tmp, "store")
        Volume.create(self.image, 4096, 16)
        manager = SnapshotManager(Volume.load(self.image), self.store)
        manager.create_snapshot("s0")
        manager.inject_hex(4, "abcd")
        self.target = manager.create_snapshot("s1")
        manager.seal_for_rollback()
        manager.close()

    def _open(self):
        return SnapshotManager(Volume.load(self.image), self.store)

    def test_crash_after_prepare_keeps_volume_untouched(self):
        with self._open() as manager:
            pre_digest = manager.volume.current_digest()
            with self.assertRaises(RuntimeError):
                rollback(manager, "s1", path=PATH_CHAIN,
                         crash_after_prepare=True)
            self.assertTrue(manager.journal.exists())
            self.assertEqual(manager.volume.current_digest(), pre_digest)

    def test_restart_recovers_and_completes(self):
        with self._open() as manager:
            with self.assertRaises(RuntimeError):
                rollback(manager, "s1", path=PATH_CHAIN,
                         crash_after_prepare=True)
        with self._open() as restarted:
            self.assertEqual(restarted.volume.current_digest(),
                             self.target.state_digest)
            self.assertFalse(restarted.journal.exists())
            self.assertTrue(restarted.detached)
            self.assertIsNone(restarted.recover_pending())


if __name__ == "__main__":
    unittest.main()
