from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from diagnostic_engine.errors import DiagnosticError
from diagnostic_engine.generator import generate_dataset
from diagnostic_engine.snapshots import (
    SnapshotCommitLock,
    downgrade_v2_to_v1,
    install_snapshot,
    load_snapshot,
    parse_v1,
    parse_v2,
    upgrade_v1_to_v2,
)


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.generated = generate_dataset(cls.root / "data", sample_count=64)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_v1_v2_parse_to_same_unified_config(self) -> None:
        v1 = parse_v1(self.generated.v1_path)
        v2 = parse_v2(self.generated.v2_path)
        self.assertEqual(v1.parameters(), v2.parameters())
        self.assertEqual(v1.version, 1)
        self.assertEqual(v2.version, 2)
        self.assertGreater(v2.created_at_ms, 1_000_000_000_000)
        self.assertEqual(
            v2.metadata_sources.get("created_at_ms"),
            "V1_ANCHOR_MS+relative_seconds*1000",
        )

    def test_upgrade_and_downgrade_with_metadata_rules(self) -> None:
        upgraded = upgrade_v1_to_v2(
            self.generated.v1_path, self.root / "upgraded"
        )
        self.assertEqual(upgraded.config.version, 2)
        self.assertTrue(upgraded.metadata_completions)
        self.assertEqual(load_snapshot(self.root / "upgraded").parameters(),
                         parse_v1(self.generated.v1_path).parameters())
        downgraded = downgrade_v2_to_v1(
            self.generated.v2_path, self.root / "downgraded.json"
        )
        self.assertEqual(downgraded.config.version, 1)
        self.assertIn("generation", downgraded.dropped_fields)
        self.assertIn("content_sha256", downgraded.dropped_fields)
        self.assertEqual(
            parse_v1(self.root / "downgraded.json").parameters(),
            parse_v2(self.generated.v2_path).parameters(),
        )

    def test_checksum_tamper_is_rejected(self) -> None:
        tampered = self.root / "tampered"
        shutil.copytree(self.generated.v2_path, tampered)
        solver_shard = tampered / "shards" / "solver.json"
        payload = json.loads(solver_shard.read_text(encoding="utf-8"))
        payload["strength_path"][0] = 999.0
        solver_shard.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(DiagnosticError) as context:
            parse_v2(tampered)
        self.assertEqual(context.exception.code, "E_SNAPSHOT_CHECKSUM")

    def test_whole_snapshot_install_rollback_and_busy(self) -> None:
        store = self.root / "store"
        first = install_snapshot(self.generated.v1_path, store)
        self.assertEqual(first.version, 1)
        self.assertEqual(first.replacement_mode, "whole_snapshot_pointer_swap")
        second = install_snapshot(self.generated.v2_path, store)
        self.assertEqual(second.version, 2)
        self.assertEqual(second.previous["version"], 1)
        no_change = install_snapshot(self.generated.v2_path, store)
        self.assertEqual(no_change.replacement_mode, "no_change")
        self.assertEqual(load_snapshot(second.active_path).version, 2)
        alternate_v1 = json.loads(
            Path(self.generated.v1_path).read_text(encoding="utf-8")
        )
        alternate_v1["learning_rate"] = 0.33
        alternate_v1_path = self.root / "alternate-v1.json"
        alternate_v1_path.write_text(json.dumps(alternate_v1), encoding="utf-8")
        upgrade_v1_to_v2(alternate_v1_path, self.root / "alternate-v2")
        replaced = install_snapshot(self.root / "alternate-v2", store)
        self.assertEqual(replaced.version, 2)
        self.assertNotEqual(replaced.checksum, second.checksum)
        self.assertEqual(
            replaced.replacement_mode, "whole_snapshot_pointer_swap"
        )
        self.assertEqual(load_snapshot(replaced.active_path).learning_rate, 0.33)
        with self.assertRaises(DiagnosticError) as rollback:
            install_snapshot(self.generated.v1_path, store)
        self.assertEqual(rollback.exception.code, "E_SNAPSHOT_ROLLBACK")
        with SnapshotCommitLock(store):
            with self.assertRaises(DiagnosticError) as busy:
                install_snapshot(self.generated.v2_path, store)
        self.assertEqual(busy.exception.code, "E_STAGE_BUSY")
        pointer = json.loads((store / "active.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["version"], 2)


if __name__ == "__main__":
    unittest.main()

