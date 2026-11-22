"""快照版本化 / 整体替换 / 损坏检测测试."""

from __future__ import annotations

import json
import os
import unittest

from genapp.errors import E_SNAPSHOT_CORRUPT, E_SNAPSHOT_STALE, GenError
from genapp.snapshot import (
    KIND_BLOCK_PLAN,
    KIND_FUSION_PARAMS,
    KIND_READY_MANIFEST,
    SnapshotStore,
)

from .helpers import temp_store


class TestSnapshot(unittest.TestCase):
    def test_submit_and_reload_roundtrip(self):
        tmp, store = temp_store()
        with tmp:
            store.submit(KIND_FUSION_PARAMS, 1, {"mix_base": 0.5})
            store.submit(KIND_FUSION_PARAMS, 2, {"mix_base": 0.6})
            reopened = SnapshotStore(tmp.name)
            record = reopened.load(KIND_FUSION_PARAMS)
            self.assertEqual(record["version"], 2)
            self.assertEqual(record["payload"], {"mix_base": 0.6})
            self.assertEqual(reopened.current_version(KIND_FUSION_PARAMS), 2)

    def test_stale_version_rejected(self):
        tmp, store = temp_store()
        with tmp:
            store.submit(KIND_BLOCK_PLAN, 3, {"grid": "2x2"})
            with self.assertRaises(GenError) as ctx:
                store.submit(KIND_BLOCK_PLAN, 2, {"grid": "1x1"})
            self.assertEqual(ctx.exception.code, E_SNAPSHOT_STALE)
            with self.assertRaises(GenError):
                store.submit(KIND_BLOCK_PLAN, 3, {"grid": "1x1"})

    def test_corrupt_checksum_detected(self):
        tmp, store = temp_store()
        with tmp:
            store.submit(KIND_READY_MANIFEST, 1, {"ready": [1, 2, 3]})
            path = os.path.join(tmp.name, f"{KIND_READY_MANIFEST}.json")
            with open(path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
            record["payload"]["ready"] = [9, 9, 9]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(record, fh)
            with self.assertRaises(GenError) as ctx:
                SnapshotStore(tmp.name).load(KIND_READY_MANIFEST)
            self.assertEqual(ctx.exception.code, E_SNAPSHOT_CORRUPT)
            self.assertEqual(ctx.exception.details["kind"], KIND_READY_MANIFEST)
            self.assertEqual(ctx.exception.details["version"], 1)

    def test_unreadable_file_detected(self):
        tmp, store = temp_store()
        with tmp:
            store.submit(KIND_BLOCK_PLAN, 1, {"grid": "1x1"})
            path = os.path.join(tmp.name, f"{KIND_BLOCK_PLAN}.json")
            with open(path, "wb") as fh:
                fh.write(b"\xff\xfe not json")
            with self.assertRaises(GenError) as ctx:
                SnapshotStore(tmp.name)
            self.assertEqual(ctx.exception.code, E_SNAPSHOT_CORRUPT)

    def test_whole_replace_not_incremental(self):
        tmp, store = temp_store()
        with tmp:
            store.submit(KIND_READY_MANIFEST, 1, {"ready": [0], "extra": 1})
            store.submit(KIND_READY_MANIFEST, 2, {"ready": [0, 1]})
            record = SnapshotStore(tmp.name).load(KIND_READY_MANIFEST)
            self.assertNotIn("extra", record["payload"])
            self.assertEqual(record["payload"], {"ready": [0, 1]})


if __name__ == "__main__":
    unittest.main()
