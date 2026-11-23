from __future__ import annotations

import os
import tempfile
import unittest

from sceneopt.cache import ArtifactCache, CommitGate, build_stage_cache_key
from sceneopt.errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    CacheCorrupt,
    SnapshotCorrupt,
    SnapshotRollback,
    StageBusy,
)
from sceneopt.snapshot import Snapshot, SnapshotStore


class CacheTests(unittest.TestCase):
    def test_cache_key_includes_all_required_fields(self) -> None:
        base = {
            "scene_content_hash": "a" * 64,
            "camera_pose_digest": "b" * 64,
            "geometry_mode": "terrain",
            "target_face_budget": 64,
            "upstream_artifact_hash": "ROOT",
            "config_digest": "c" * 64,
            "effective_face_budget": 64,
        }
        original = build_stage_cache_key("ingest_register", **base)
        self.assertEqual(
            original, build_stage_cache_key("ingest_register", **base)
        )
        mutations = {
            "scene_content_hash": "d" * 64,
            "camera_pose_digest": "e" * 64,
            "geometry_mode": "ellipsoid",
            "target_face_budget": 32,
            "upstream_artifact_hash": "f" * 64,
            "config_digest": "1" * 64,
        }
        for field, value in mutations.items():
            changed = dict(base)
            changed[field] = value
            self.assertNotEqual(
                original,
                build_stage_cache_key("ingest_register", **changed),
                field,
            )

    def test_cache_round_trip_and_corruption_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = ArtifactCache(directory)
            key = "0" * 64
            checksum = cache.put("summary_verify", key, {"value": 3.5})
            payload = cache.get("summary_verify", key)
            self.assertEqual(payload, {"value": 3.5})
            self.assertEqual(len(checksum), 64)
            path = cache._path("summary_verify", key)
            with open(path, "ab") as handle:
                handle.write(b"corrupt")
            with self.assertRaises(CacheCorrupt):
                cache.get("summary_verify", key)

    def test_commit_gate_is_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = os.path.join(directory, "commit.lock")
            first = CommitGate(lock_path)
            second = CommitGate(lock_path)
            first.acquire()
            try:
                with self.assertRaises(StageBusy) as context:
                    second.acquire()
                self.assertEqual(context.exception.code, E_STAGE_BUSY)
            finally:
                first.release()


class SnapshotTests(unittest.TestCase):
    def test_whole_replacement_rollback_and_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "snapshot.json")
            store = SnapshotStore(path)
            first = Snapshot.create(
                version=1,
                config_digest="config-1",
                scenes={"a": {"value": 1}},
            )
            first_result = store.commit(first)
            self.assertTrue(first_result.committed)
            self.assertFalse(first_result.replaced)

            second = Snapshot.create(
                version=2,
                config_digest="config-2",
                scenes={"b": {"value": 2}},
            )
            second_result = store.commit(second)
            self.assertTrue(second_result.committed)
            self.assertTrue(second_result.replaced)
            self.assertTrue(second_result.checksum_changed)

            loaded = store.load()
            self.assertEqual(set(loaded.scenes), {"b"})

            with self.assertRaises(SnapshotRollback) as context:
                store.commit(first)
            self.assertEqual(context.exception.code, E_SNAPSHOT_ROLLBACK)
            self.assertEqual(context.exception.details["current_version"], 2)

            with open(path, "wb") as handle:
                handle.write(b'{"format":"broken"}')
            with self.assertRaises(SnapshotCorrupt) as context:
                store.load()
            self.assertEqual(context.exception.code, E_SNAPSHOT_CORRUPT)


if __name__ == "__main__":
    unittest.main()

