import json
import tempfile
import unittest
from pathlib import Path

from recall_fusion.errors import ErrorCode, PipelineError
from recall_fusion.snapshot import Snapshot, SnapshotStore, compute_checksum


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.store = SnapshotStore()

    def test_atomic_whole_replacement_and_monotonic_versions(self):
        v1 = Snapshot.create("fusion_weights", 1, {"inverted": 1.0})
        v2 = Snapshot.create("fusion_weights", 2, {"inverted": 0.5, "vector": 2.0})
        self.store.submit(v1)
        self.store.submit(v2)
        self.assertIs(self.store.current("fusion_weights"), v2)
        self.assertEqual(self.store.current("fusion_weights").payload, v2.payload)

    def test_rollback_is_rejected(self):
        self.store.submit(Snapshot.create("recall_stats", 3, {"inverted": {}}))
        older = Snapshot.create("recall_stats", 2, {"inverted": {}})
        with self.assertRaises(PipelineError) as ctx:
            self.store.submit(older)
        self.assertEqual(ctx.exception.code, ErrorCode.E_SNAPSHOT_ROLLBACK)
        self.assertEqual(ctx.exception.context["current_version"], 3)
        # Old version never replaced the current one.
        self.assertEqual(self.store.current("recall_stats").version, 3)

    def test_corrupt_checksum_detected_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Snapshot.create("normalization_params", 1, {"vector": {}})
            snapshot.save(tmp)
            path = Path(tmp) / "normalization_params.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["payload"]["vector"]["normalization"] = "tampered"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(PipelineError) as ctx:
                Snapshot.load(tmp, "normalization_params")
            self.assertEqual(ctx.exception.code, ErrorCode.E_SNAPSHOT_CORRUPT)
            self.assertEqual(ctx.exception.context["version"], 1)

    def test_corrupt_json_detected_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "recall_stats.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(PipelineError) as ctx:
                Snapshot.load(tmp, "recall_stats")
            self.assertEqual(ctx.exception.code, ErrorCode.E_SNAPSHOT_CORRUPT)

    def test_roundtrip_through_store_and_disk(self):
        snapshot = Snapshot.create("fusion_weights", 1, {"vector": 1.5})
        with tempfile.TemporaryDirectory() as tmp:
            snapshot.save(tmp)
            reloaded = self.store.load_into(tmp, ["fusion_weights"])["fusion_weights"]
            self.assertEqual(reloaded.payload, snapshot.payload)
            self.assertEqual(reloaded.checksum, compute_checksum(snapshot.payload))

    def test_same_version_resubmission_with_new_content_replaces(self):
        # Equal version is allowed (reload of identical snapshot); content still
        # replaced atomically as a whole object.
        a = Snapshot.create("fusion_weights", 1, {"vector": 1.0})
        b = Snapshot.create("fusion_weights", 1, {"vector": 2.0})
        self.store.submit(a)
        self.store.submit(b)
        self.assertEqual(self.store.current("fusion_weights").payload, {"vector": 2.0})


if __name__ == "__main__":
    unittest.main()
