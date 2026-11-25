from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from feature_diag.config import normalize_config
from feature_diag.engine import compute
from feature_diag.errors import E_SNAPSHOT_ROLLBACK, EngineError
from feature_diag.generator import prepare_workspace
from feature_diag.sample import read_rows_csv
from feature_diag.snapshot import (
    configs_semantically_equal,
    load_v1,
    load_v2,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
)
from feature_diag.util import canonical_json, read_json, sha256_bytes


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        prepare_workspace(self.root)
        self.v1_path = self.root / "configs" / "v1.json"
        self.v2_path = self.root / "configs" / "v2"
        self.v1 = load_v1(self.v1_path)
        self.v2 = load_v2(self.v2_path)
        self.rows = read_rows_csv(self.root / "data" / "sample.csv", self.v2)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_migration_preserves_selection(self) -> None:
        self.assertTrue(configs_semantically_equal(self.v1, self.v2))
        v1_result = compute(self.rows, self.v1)
        v2_result = compute(self.rows, self.v2)
        self.assertEqual(v1_result.selection_key, v2_result.selection_key)
        self.assertEqual(v1_result.matrix_digest, v2_result.matrix_digest)
        self.assertEqual(v1_result.retained_feature_ids, v2_result.retained_feature_ids)

        roundtrip_v1_path = self.root / "work" / "roundtrip-v1.json"
        roundtrip_v1, _ = migrate_v2_to_v1(self.v2_path, roundtrip_v1_path, target_version=2)
        roundtrip_v2_path = self.root / "work" / "roundtrip-v2"
        roundtrip_v2, _ = migrate_v1_to_v2(roundtrip_v1_path, roundtrip_v2_path, target_version=2)
        self.assertTrue(configs_semantically_equal(self.v1, roundtrip_v1))
        self.assertTrue(configs_semantically_equal(self.v2, roundtrip_v2))
        self.assertEqual(compute(self.rows, roundtrip_v1).selection_key, v2_result.selection_key)
        self.assertEqual(compute(self.rows, roundtrip_v2).selection_key, v2_result.selection_key)
        self.assertTrue(roundtrip_v2.completion_sources)
        for entry in roundtrip_v2.completion_sources:
            self.assertIn("field", entry)
            self.assertIn("action", entry)
            self.assertIn("source", entry)

    def test_checksum_mismatch_is_rejected(self) -> None:
        with self.assertRaises(EngineError) as caught:
            load_v1(self.v1_path, checksum="0" * 64)
        self.assertEqual(caught.exception.code, E_SNAPSHOT_ROLLBACK)
        self.assertEqual(caught.exception.detail["field"], "checksum")

    def test_unparseable_field_is_rejected_as_snapshot_rollback(self) -> None:
        (self.root / "work").mkdir(parents=True, exist_ok=True)
        raw = read_json(self.v1_path)
        raw["selector"]["target_k"] = "bad"
        data = canonical_json(raw, newline=True).encode("utf-8")
        bad_path = self.root / "work" / "bad-v1.json"
        bad_path.write_bytes(data)
        bad_path.with_name(bad_path.name + ".sha256").write_text(
            sha256_bytes(data) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(EngineError) as caught:
            load_v1(bad_path)
        self.assertEqual(caught.exception.code, E_SNAPSHOT_ROLLBACK)

    def test_version_rollback_is_rejected_without_partial_output(self) -> None:
        (self.root / "work").mkdir(parents=True, exist_ok=True)
        output = self.root / "work" / "rollback-v1.json"
        output.write_text("sentinel", encoding="utf-8")
        with self.assertRaises(EngineError) as caught:
            migrate_v2_to_v1(self.v2_path, output, target_version=1)
        self.assertEqual(caught.exception.code, E_SNAPSHOT_ROLLBACK)
        self.assertEqual(output.read_text(encoding="utf-8"), "sentinel")

    def test_v2_shard_corruption_is_rejected(self) -> None:
        shard = self.v2_path / "cross.json"
        shard.write_bytes(shard.read_bytes() + b" ")
        with self.assertRaises(EngineError) as caught:
            load_v2(self.v2_path)
        self.assertEqual(caught.exception.code, E_SNAPSHOT_ROLLBACK)


if __name__ == "__main__":
    unittest.main()
