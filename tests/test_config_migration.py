from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dsn_workbench.config import (
    ConfigBundle,
    migrate_config,
    read_config,
    write_v1,
    write_v2,
)
from dsn_workbench.errors import (
    E_FORMAT_CORRUPT,
    E_VERSION_UNSUPPORTED,
    WorkbenchError,
)
from dsn_workbench.sample import default_config_data
from dsn_workbench.util import atomic_write_json, content_hash, read_json_file


class ConfigMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle = ConfigBundle(
            data=default_config_data(),
            format_version=1,
            source="generated:test",
            provenance={
                key: "test"
                for key in (
                    "dictionary_version",
                    "rule_version",
                    "workers",
                    "batch_size",
                    "logical_clock_ms",
                    "generation",
                )
            },
        )
        self.v1_path = self.root / "v1.json"
        write_v1(self.bundle, self.v1_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_v1_v2_roundtrip(self) -> None:
        v1 = read_config(self.v1_path)
        self.assertEqual(v1.format_version, 1)
        v2_path = self.root / "v2"
        write_v2(v1, v2_path)
        v2 = read_config(v2_path)
        self.assertEqual(v2.format_version, 2)
        for key in (
            "dictionary",
            "rules",
            "domain_tags",
            "domain_pos_table",
            "tagger",
            "limits",
            "normalization",
        ):
            self.assertEqual(v1.data[key], v2.data[key])
        self.assertEqual(
            v1.data["dictionary_version"], v2.data["dictionary_version"]
        )

        connection = sqlite3.connect(v2_path / "metadata.sqlite3")
        try:
            rows = dict(
                connection.execute("SELECT key, value FROM meta").fetchall()
            )
            shards = dict(
                connection.execute(
                    "SELECT name, sha256 FROM shards"
                ).fetchall()
            )
        finally:
            connection.close()
        self.assertEqual(json.loads(rows["workers"]), 2)
        self.assertEqual(
            json.loads(rows["dictionary_version"]), "lex-2026-11-27.1"
        )
        self.assertIn("dictionary", shards)
        self.assertEqual(len(shards["dictionary"]), 64)

        downgraded = self.root / "downgraded.json"
        report = migrate_config(v2_path, downgraded, 1, overwrite=True)
        self.assertEqual(report.target_version, 1)
        self.assertTrue(
            any(item["field"] == "metadata_db.shards" for item in report.dropped)
        )
        final = read_config(downgraded)
        self.assertNotIn("metadata_extensions", final.data)
        self.assertEqual(final.data["dictionary"], v1.data["dictionary"])

    def test_upgrade_records_field_provenance_and_checksums(self) -> None:
        target = self.root / "upgraded"
        report = migrate_config(self.v1_path, target, 2, overwrite=True)
        fields = {item["field"] for item in report.audit}
        self.assertIn("metadata_db.dictionary_version", fields)
        self.assertIn("metadata_db.rule_version", fields)
        self.assertIn("shards.checksum", fields)
        self.assertIn("manifest.checksum", fields)
        upgraded = read_config(target)
        self.assertEqual(upgraded.format_version, 2)
        self.assertIn("migration_audit", upgraded.data)

    def test_unknown_version_returns_structured_code(self) -> None:
        path = self.root / "future.json"
        atomic_write_json(path, {"format_version": 99, "anything": True})
        with self.assertRaises(WorkbenchError) as context:
            read_config(path)
        self.assertEqual(context.exception.code, E_VERSION_UNSUPPORTED)
        self.assertEqual(context.exception.details["format_version"], 99)

    def test_missing_header_is_corrupt(self) -> None:
        path = self.root / "missing.json"
        atomic_write_json(path, {"locale": "zh-CN"})
        with self.assertRaises(WorkbenchError) as context:
            read_config(path)
        self.assertEqual(context.exception.code, E_FORMAT_CORRUPT)

    def test_unparseable_number_is_corrupt(self) -> None:
        raw = read_json_file(self.v1_path)
        raw["workers"] = "many"
        raw.pop("content_checksum", None)
        path = self.root / "bad-number.json"
        atomic_write_json(path, raw)
        with self.assertRaises(WorkbenchError) as context:
            read_config(path)
        self.assertEqual(context.exception.code, E_FORMAT_CORRUPT)

    def test_v1_checksum_tamper_is_corrupt(self) -> None:
        raw = read_json_file(self.v1_path)
        raw["batch_size"] += 1
        path = self.root / "tampered.json"
        atomic_write_json(path, raw)
        with self.assertRaises(WorkbenchError) as context:
            read_config(path)
        self.assertEqual(context.exception.code, E_FORMAT_CORRUPT)
        self.assertIn("checksum", context.exception.message)

    def test_v2_shard_tamper_is_corrupt(self) -> None:
        target = self.root / "v2-shard"
        write_v2(read_config(self.v1_path), target)
        dictionary_path = target / "dictionary.json"
        payload = read_json_file(dictionary_path)
        payload["dictionary"][0]["term"] = "tampered"
        atomic_write_json(dictionary_path, payload)
        with self.assertRaises(WorkbenchError) as context:
            read_config(target)
        self.assertEqual(context.exception.code, E_FORMAT_CORRUPT)
        self.assertIn("shard checksum", context.exception.message)

    def test_v2_only_fields_are_dropped_with_explicit_rules(self) -> None:
        v2_path = self.root / "v2-extra"
        data = copy.deepcopy(self.bundle.data)
        data["metadata_extensions"] = {"snapshot_id": "x"}
        data["migration_audit"] = [{"field": "x"}]
        write_v2(
            ConfigBundle(
                data=data,
                format_version=2,
                source="test",
                provenance=self.bundle.provenance,
            ),
            v2_path,
        )
        target = self.root / "extra-v1.json"
        report = migrate_config(v2_path, target, 1, overwrite=True)
        dropped = {item["field"]: item["rule"] for item in report.dropped}
        self.assertIn("metadata_extensions", dropped)
        self.assertIn("V1_DROP_RULE", dropped["metadata_extensions"])
        persisted = read_config(target)
        self.assertNotIn("metadata_extensions", persisted.data)
        self.assertNotIn("migration_audit", persisted.data)

    def test_generated_checksum_is_content_addressed(self) -> None:
        raw = read_json_file(self.v1_path)
        checksum = raw.pop("content_checksum")
        self.assertEqual(checksum, content_hash(raw))


if __name__ == "__main__":
    unittest.main()

