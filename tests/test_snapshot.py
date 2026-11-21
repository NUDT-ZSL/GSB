import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from mmpipe.config import PipelineConfig
from mmpipe.errors import SnapshotCorruptError
from mmpipe.store import SnapshotStore


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = PipelineConfig()
        self.store = SnapshotStore(self.root, self.config)
        self.pairs = ["p0", "p1", "p2"]

    def tearDown(self):
        self.tmp.cleanup()

    def _commit(self, stage="image", version=1):
        staging = self.store.begin_stage(stage, version)
        for pair_id in self.pairs:
            self.store.write_embedding(staging, pair_id, np.arange(4, dtype=np.float32))
        return self.store.commit_stage(stage, staging, self.pairs, version)

    def test_commit_only_visible_with_marker_and_version_monotonic(self):
        snapshot = self._commit()
        self.assertTrue((snapshot.path / "COMMITTED").is_file())
        self.assertEqual(snapshot.version, 1)
        loaded = self.store.load_committed("image")
        self.assertEqual(loaded.pair_ids, tuple(sorted(self.pairs)))
        self.assertEqual(self.store.next_version("image"), 2)

    def test_uncommitted_staging_is_discarded_on_recovery(self):
        staging = self.store.begin_stage("image", 1)
        self.store.write_embedding(staging, "p0", np.ones(2, dtype=np.float32))
        # No commit: a fresh store must ignore and clean the staging area.
        self.assertIsNone(SnapshotStore(self.root, self.config).load_committed("image"))
        self.assertFalse(list((self.root / "snapshots" / "image").glob(".tmp-*")))

    def test_corrupt_checksum_raises_with_stage_and_version(self):
        self._commit(version=1)
        target = self.root / "snapshots" / "image" / "v1" / "embeddings" / "p0.npy"
        raw = target.read_bytes()
        target.write_bytes(raw[: len(raw) // 2])
        with self.assertRaises(SnapshotCorruptError) as ctx:
            self.store.load_committed("image")
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(ctx.exception.stage, "image")
        self.assertEqual(ctx.exception.version, 1)

    def test_manifest_tamper_detection(self):
        self._commit(version=1)
        manifest_path = self.root / "snapshots" / "image" / "v1" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["embeddings_checksum"] = "f" * 64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(SnapshotCorruptError):
            self.store.load_committed("image")

    def test_version_change_replaces_old_snapshot(self):
        first = self._commit(version=1)
        staging = self.store.begin_stage("image", 2)
        for pair_id in self.pairs:
            self.store.write_embedding(staging, pair_id, np.full(4, 2.0, dtype=np.float32))
        second = self.store.commit_stage("image", staging, self.pairs, 2)
        self.assertFalse(first.path.exists())
        self.assertTrue(second.path.exists())
        loaded = self.store.load_committed("image")
        self.assertEqual(loaded.version, 2)
        self.assertEqual(loaded.embeddings_checksum, second.embeddings_checksum)

    def test_force_rebuild_recovers_from_corruption(self):
        self._commit(version=1)
        target = self.root / "snapshots" / "image" / "v1" / "embeddings" / "p1.npy"
        target.write_bytes(target.read_bytes()[:8])
        self.assertIsNone(self.store.load_committed("image", force_rebuild=True))
        rebuilt = self._commit(version=1)
        self.assertTrue(rebuilt.path.exists())

    def test_config_digest_change_is_corrupt(self):
        self._commit(version=1)
        other = SnapshotStore(self.root, PipelineConfig(image_dim=4))
        with self.assertRaises(SnapshotCorruptError):
            other.load_committed("image")


if __name__ == "__main__":
    unittest.main()

