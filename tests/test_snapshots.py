"""要求 6：两代配置快照迁移、整体替换与回退保护。"""

import os

from statediag.errors import ESnapshotRollback
from statediag.model import TransformConfig
from statediag.snapshots import (
    guard_no_rollback,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    read_v1,
    read_v2,
    write_v1,
    write_v2,
)

from tests.support import SampleCase


class SnapshotTest(SampleCase):
    def test_v1_uses_relative_seconds_flat(self):
        cfg = TransformConfig(1, "float64", "half-even", 1e-12, 1e-9,
                              self.origin_ms + 5000)
        path = write_v1(self.new_workdir(), cfg, origin_ms=self.origin_ms)
        payload = read_v1(path, origin_ms=self.origin_ms)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["effective_ms"], self.origin_ms + 5000)
        self.assertEqual(payload["algebra"], 1)

    def test_v2_absolute_ms_algebra_tombstones_sqlite(self):
        cfg = TransformConfig(3, "fixed", "truncate", 1e-2, 1e-2,
                              self.origin_ms + 9000)
        out = self.new_workdir()
        tombstones = [{"tombstone_id": "tb1", "algebra": 3, "reason": "r"}]
        write_v2(out, cfg, tombstones=tombstones)
        payload = read_v2(out)
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["effective_ms"], self.origin_ms + 9000)
        self.assertEqual(payload["algebra"], 3)
        self.assertEqual(payload["tombstones"], tombstones)
        self.assertTrue(os.path.exists(os.path.join(out,
                                                    "config_v2.meta.sqlite")))

    def test_migrate_v1_to_v2_provenance(self):
        v1_path = os.path.join(self.sample_dir, "snapshots", "v1",
                               "config_v1.json")
        out = self.new_workdir()
        _, provenance = migrate_v1_to_v2(
            out, v1_path, origin_ms=self.origin_ms,
            tombstones=[{"tombstone_id": "tb_x", "algebra": 1,
                         "reason": "x"}],
        )
        fields = {p["field"] for p in provenance}
        self.assertEqual(
            fields, {"effective_ms", "algebra", "tombstones"}
        )
        v2 = read_v2(out)
        self.assertEqual(v2["algebra"], 1)
        self.assertEqual(len(v2["tombstones"]), 1)

    def test_downgrade_explicitly_drops_tombstones(self):
        v2_dir = os.path.join(self.sample_dir, "snapshots", "v2")
        out = self.new_workdir()
        path, dropped = migrate_v2_to_v1(
            out, v2_dir, origin_ms=self.origin_ms
        )
        self.assertTrue(dropped)
        for item in dropped:
            self.assertIn("explicitly_dropped", item["rule"])
        payload = read_v1(path, origin_ms=self.origin_ms)
        self.assertEqual(payload["version"], 1)

    def test_rollback_rejected(self):
        v2 = read_v2(os.path.join(self.sample_dir, "snapshots", "v2"))
        v1 = read_v1(
            os.path.join(self.sample_dir, "snapshots", "v1",
                         "config_v1.json"),
            origin_ms=self.origin_ms,
        )
        with self.assertRaises(ESnapshotRollback) as ctx:
            guard_no_rollback(v2, v1)
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_ROLLBACK")

    def test_same_version_checksum_change_is_full_replace(self):
        v1 = read_v1(
            os.path.join(self.sample_dir, "snapshots", "v1",
                         "config_v1.json"),
            origin_ms=self.origin_ms,
        )
        candidate = dict(v1)
        candidate["checksum"] = "different"
        self.assertTrue(guard_no_rollback(v1, candidate))
