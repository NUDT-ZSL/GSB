import unittest

from kfdiff.errors import KFError
from tests.helpers import generate_all, make_studio


class CacheSnapshotTests(unittest.TestCase):
    def test_content_addressed_objects_and_cache_key_fields(self):
        studio, _ = make_studio()
        result = studio.generate("clip_0001", parallel=False)
        manifest = studio.snapshots.current_manifest("clip_0001")
        self.assertTrue(studio.store.has(manifest["manifest_hash"]))
        for artifact in manifest["artifacts"]:
            self.assertTrue(studio.store.has(artifact["content_hash"]), artifact["name"])

        entry = studio.db.get_cache_entry(manifest["keyframes"][0]["cache_key"])
        self.assertIsNotNone(entry)
        self.assertEqual(entry["clip_id"], "clip_0001")
        self.assertEqual(entry["revision"], 1)
        self.assertEqual(entry["block_grid"], result["plan"]["selected_block_grid"])
        self.assertTrue(entry["upstream_sig"])

    def test_wholesale_versions_and_rollback_rejected(self):
        studio, _ = make_studio()
        studio.generate("clip_0001", parallel=False)
        studio.apply_revision_event("evt_rev2_prompt_embedding")
        # prompt event targets clip_0004; craft a clip_0001 revision directly
        studio.tasks["clip_0001"].revision = 2
        studio.task_rows["clip_0001"]["revision"] = 2
        studio.generate("clip_0001", parallel=False)
        versions = [v["version"] for v in studio.snapshots.list_versions("clip_0001")]
        self.assertEqual(versions, [1, 2])

        with self.assertRaises(KFError) as ctx:
            studio.snapshots.request_rollback("clip_0001", 1)
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_ROLLBACK")
        self.assertEqual(studio.snapshots.current_version("clip_0001"), 2)

    def test_stale_writer_rejected(self):
        studio, _ = make_studio()
        studio.generate("clip_0001", parallel=False)
        with self.assertRaises(KFError) as ctx:
            studio.snapshots.commit_new("clip_0001", 1, {"x": 1},
                                        expected_base_version=0)
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_ROLLBACK")

    def test_forward_restore_creates_new_version(self):
        studio, _ = make_studio()
        studio.generate("clip_0001", parallel=False)
        studio.tasks["clip_0001"].revision = 2
        studio.generate("clip_0001", parallel=False)
        restored = studio.snapshots.restore_version_forward("clip_0001", 1)
        self.assertEqual(restored["version"], 3)
        self.assertEqual(restored["restored_from_version"], 1)

    def test_checksum_tamper_detected(self):
        studio, _ = make_studio()
        studio.generate("clip_0001", parallel=False)
        manifest = studio.snapshots.current_manifest("clip_0001")
        digest = manifest["keyframes"][0]["content_hash"]
        studio.store._path_for(digest).write_bytes(b"corrupt")
        with self.assertRaises(KFError) as ctx:
            studio.snapshots.verify_current("clip_0001")
        self.assertEqual(ctx.exception.code, "E_CHECKSUM_MISMATCH")

    def test_second_full_pass_recomputes_nothing(self):
        studio, _ = make_studio()
        generate_all(studio)
        for clip_id in sorted(studio.tasks):
            result = studio.generate(clip_id, parallel=False)
            self.assertEqual(result["propagation"]["recomputed_keyframes"], [])
            self.assertTrue(
                all(k["reuse"] == "exact_hit"
                    for k in result["propagation"]["reused_keyframes"])
            )


if __name__ == "__main__":
    unittest.main()
