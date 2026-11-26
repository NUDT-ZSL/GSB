from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mm_router.canonical import atomic_write_json, sha256_file
from mm_router.config_io import read_config, write_v1_config, write_v2_config
from mm_router.errors import (
    E_FORMAT_CORRUPT,
    E_SEMANTIC_LOSS,
    E_VERSION_UNSUPPORTED,
    WorkbenchError,
)
from mm_router.generate import generate_sample
from mm_router.migration import downgrade_v2_to_v1, migrate_v1_to_v2
from mm_router.models import ThresholdConfig, Tombstone


class ConfigMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_v1_upgrade_fills_fields_with_per_item_provenance(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        loaded = read_config(sample["config_v1"])
        self.assertEqual(loaded.format_version, 1)
        self.assertEqual(loaded.config.generation, 1)
        self.assertEqual(loaded.config.revision, 0)
        self.assertEqual(loaded.config.tombstones, ())
        self.assertEqual(
            loaded.config.created_at_ms,
            loaded.config.updated_at_ms,
        )
        provenance_fields = {record.field for record in loaded.provenance}
        for field in (
            "updated_at_ms",
            "generation",
            "revision",
            "tombstones",
            "entries.image.revision",
            "entries.image.updated_at_ms",
            "entries.joint.revision",
            "entries.text.revision",
        ):
            self.assertIn(field, provenance_fields)

    def test_v1_to_v2_written_shards_read_back_field_identically(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        source = json.loads(Path(sample["config_v1"]).read_text(encoding="utf-8"))
        migration = migrate_v1_to_v2(source)
        target = write_v2_config(migration.config, self.root / "v2")
        loaded = read_config(target)
        self.assertEqual(loaded.format_version, 2)
        self.assertEqual(
            loaded.config.semantic_dict(),
            migration.config.semantic_dict(),
        )
        self.assertEqual(len(loaded.shard_checksums), 3)
        self.assertIsNotNone(loaded.metadata_checksum)

    def test_v2_downgrade_records_revision_discards(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        config = read_config(sample["config_v2"]).config
        result = downgrade_v2_to_v1(config)
        self.assertEqual(result.business_code, "OK")
        self.assertFalse(result.semantic_equivalent)
        fields = {record.field for record in result.discard_records}
        self.assertIn("generation", fields)
        self.assertIn("revision", fields)
        for key in ("image", "joint", "text"):
            self.assertIn(f"entries.{key}.revision", fields)
        assert result.document is not None
        path = write_v1_config(result.document, self.root / "downgraded.json")
        downgraded = read_config(path)
        self.assertEqual(
            downgraded.config.threshold_map(),
            config.threshold_map(),
        )
        self.assertEqual(downgraded.config.route_budget, config.route_budget)
        self.assertEqual(
            result.config.semantic_dict(),
            downgraded.config.semantic_dict(),
        )

    def test_tombstone_makes_downgrade_semantically_lossy(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        config = read_config(sample["config_v2"]).config
        with_tombstone = ThresholdConfig(
            schema_version=2,
            generation=config.generation,
            revision=config.revision,
            created_at_ms=config.created_at_ms,
            updated_at_ms=config.updated_at_ms,
            entries=config.entries,
            tombstones=(
                Tombstone(
                    key="legacy_text",
                    revision=7,
                    invalidated_at_ms=config.updated_at_ms,
                ),
            ),
            route_budget=config.route_budget,
            candidate_plan_count=config.candidate_plan_count,
            scheduler_workers=config.scheduler_workers,
            precision=config.precision,
        )
        with self.assertRaises(WorkbenchError) as caught:
            downgrade_v2_to_v1(with_tombstone)
        self.assertEqual(caught.exception.code, E_SEMANTIC_LOSS)

    def test_unknown_version_is_rejected(self) -> None:
        path = self.root / "unknown.json"
        atomic_write_json(path, {"format_version": 99, "config": {}})
        with self.assertRaises(WorkbenchError) as caught:
            read_config(path)
        self.assertEqual(caught.exception.code, E_VERSION_UNSUPPORTED)

    def test_missing_field_is_not_defaulted(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        source = json.loads(Path(sample["config_v1"]).read_text(encoding="utf-8"))
        del source["config"]["precision"]
        path = self.root / "missing.json"
        atomic_write_json(path, source)
        with self.assertRaises(WorkbenchError) as caught:
            read_config(path)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)

    def test_missing_header_dispatches_v1_by_field_set(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        source = json.loads(Path(sample["config_v1"]).read_text(encoding="utf-8"))
        del source["format_version"]
        path = self.root / "no-header-v1.json"
        atomic_write_json(path, source)
        loaded = read_config(path)
        self.assertEqual(loaded.format_version, 1)

    def test_missing_header_v1_missing_field_is_format_corrupt(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        source = json.loads(Path(sample["config_v1"]).read_text(encoding="utf-8"))
        del source["format_version"]
        del source["config"]["precision"]
        path = self.root / "no-header-bad-v1.json"
        atomic_write_json(path, source)
        with self.assertRaises(WorkbenchError) as caught:
            read_config(path)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)

    def test_missing_header_dispatches_v2_by_field_set(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        directory = Path(sample["config_v2"])
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        del manifest["format_version"]
        atomic_write_json(manifest_path, manifest)
        loaded = read_config(directory)
        self.assertEqual(loaded.format_version, 2)

    def test_unparsable_numeric_is_format_corrupt(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        source = json.loads(Path(sample["config_v1"]).read_text(encoding="utf-8"))
        source["config"]["thresholds"]["text"] = "NaN-ish"
        path = self.root / "bad-number.json"
        atomic_write_json(path, source)
        with self.assertRaises(WorkbenchError) as caught:
            read_config(path)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)

    def test_v1_timestamp_rollback_is_rejected(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        loaded = read_config(sample["config_v1"])
        with self.assertRaises(WorkbenchError) as caught:
            read_config(
                sample["config_v1"],
                previous_updated_at_ms=loaded.config.updated_at_ms + 1,
            )
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)

    def test_v2_timestamp_rollback_is_rejected(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        config = read_config(sample["config_v2"]).config
        with self.assertRaises(WorkbenchError) as caught:
            read_config(
                sample["config_v2"],
                previous_updated_at_ms=config.updated_at_ms + 1,
            )
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)

    def test_v2_shard_checksum_corruption_is_rejected(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        shard = Path(sample["config_v2"]) / "shards" / "text.json"
        shard.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(WorkbenchError) as caught:
            read_config(sample["config_v2"])
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)

    def test_v2_sqlite_manifest_conflict_is_rejected(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=3)
        directory = Path(sample["config_v2"])
        metadata_path = directory / "metadata.sqlite3"
        connection = sqlite3.connect(str(metadata_path))
        try:
            connection.execute(
                "UPDATE config_meta SET value=? WHERE key=?",
                ("3", "config_generation"),
            )
            connection.commit()
        finally:
            connection.close()
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["metadata_checksum"] = sha256_file(metadata_path)
        atomic_write_json(manifest_path, manifest)
        with self.assertRaises(WorkbenchError) as caught:
            read_config(directory)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)

    def test_fixed_seed_sample_files_are_byte_deterministic(self) -> None:
        first = generate_sample(self.root / "first", request_count=5)
        second = generate_sample(self.root / "second", request_count=5)

        def hashes(root: str) -> dict[str, str]:
            base = Path(root)
            return {
                str(path.relative_to(base)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(base.rglob("*"))
                if path.is_file()
            }

        self.assertEqual(hashes(first["root"]), hashes(second["root"]))


if __name__ == "__main__":
    unittest.main()

