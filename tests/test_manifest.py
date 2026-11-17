import os
import stat
import tempfile
import unittest

from blocksnap.errors import ChainError, SnapshotError
from blocksnap.manager import SnapshotManager
from blocksnap.manifest import Manifest
from blocksnap.volume import Volume


class ManifestReplacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.image = os.path.join(self.tmp, "v.bin")
        self.store = os.path.join(self.tmp, "store")
        Volume.create(self.image, 4096, 16)
        self.manager = SnapshotManager(Volume.load(self.image), self.store)
        self.manager.create_snapshot("a")
        self.manager.inject_hex(1, "aa")
        self.manager.create_snapshot("b")
        self.manager.seal_for_rollback()

    def tearDown(self):
        self.manager.close()

    def test_version_change_replaces_whole_manifest(self):
        old = self.manager.manifest
        import copy
        new = Manifest(
            format_version=old.format_version + 1,
            sequence=len(old.snapshots),
            block_size=old.block_size,
            block_count=old.block_count,
            baseline_object=old.baseline_object,
            snapshots=copy.deepcopy(old.snapshots),
        )
        report = self.manager.replace_manifest(new)
        self.assertTrue(report["replaced"])
        self.assertTrue(report["version_changed"])
        self.assertEqual(len(report["affected_snapshots"]), 2)
        backup = report["backup_manifest"]
        self.assertTrue(os.path.exists(backup))
        mode = stat.S_IMODE(os.stat(backup).st_mode)
        self.assertEqual(mode & stat.S_IWUSR, 0)
        self.assertEqual(self.manager.manifest.format_version,
                         old.format_version + 1)

    def test_no_change_is_noop(self):
        import copy
        same = copy.deepcopy(self.manager.manifest)
        report = self.manager.replace_manifest(same)
        self.assertFalse(report["replaced"])

    def test_missing_bundle_object_is_chain_error(self):
        old = self.manager.manifest
        import copy
        new = Manifest(
            format_version=old.format_version + 1,
            sequence=1,
            block_size=old.block_size,
            block_count=old.block_count,
            baseline_object="sha256-" + "0" * 64,
            snapshots=[],
        )
        with self.assertRaises(ChainError):
            self.manager.replace_manifest(new, {})

    def test_tampered_manifest_digest_detected(self):
        path = os.path.join(self.store, "manifest.json")
        import json
        payload = json.load(open(path))
        payload["sequence"] = 999
        json.dump(payload, open(path, "w"))
        with self.assertRaises(SnapshotError):
            SnapshotManager(Volume.load(self.image), self.store)


if __name__ == "__main__":
    unittest.main()
