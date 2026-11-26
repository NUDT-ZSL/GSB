from __future__ import annotations

import sqlite3
import tempfile
import unittest
import json
from pathlib import Path

from index_diag.config import (
    ConfigLoader,
    downgrade_v2_to_v1,
    load_config,
    upgrade_v1_to_v2,
)
from index_diag.errors import (
    DiagnosticError,
    E_CONFIG_ROLLBACK,
    E_VERSION_UNSUPPORTED,
)
from index_diag.util import canonical_json, sha256_text

from .helpers import make_config, write_json


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.v1 = self.root / "v1.json"
        write_json(self.v1, make_config(generation=3))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_error(self, code: str, function, *args, **kwargs) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_v1_upgrade_downgrade_round_trip(self) -> None:
        original = ConfigLoader.load(self.v1).config
        v1_without_resources = make_config(generation=3)
        v1_without_resources.pop("resource_limits")
        no_resources_path = self.root / "v1-no-resources.json"
        write_json(no_resources_path, v1_without_resources)
        result = upgrade_v1_to_v2(no_resources_path, self.root / "v2", shard_count=2)
        upgraded = load_config(result.target_path)
        self.assertEqual(upgraded.schema_version, 2)
        self.assertEqual(upgraded.generation, original.generation)
        self.assertEqual(upgraded.vocabulary, original.vocabulary)
        self.assertEqual(upgraded.chunk_size, original.chunk_size)
        self.assertEqual(upgraded.compression, original.compression)
        self.assertEqual(upgraded.delta_step, original.delta_step)
        self.assertTrue(upgraded.migration_history)
        self.assertIn("resource_limits", result.report["completed_default_fields"])
        self.assertTrue((self.root / "v2" / "metadata.sqlite").exists())

        downgraded = downgrade_v2_to_v1(
            result.target_path, self.root / "downgraded.json"
        )
        restored = load_config(downgraded.target_path)
        self.assertEqual(restored.schema_version, 1)
        self.assertEqual(restored.generation, original.generation)
        self.assertEqual(restored.vocabulary, original.vocabulary)
        self.assertIn("metadata_db", downgraded.report["dropped_fields"])
        self.assertIn("shard_clocks", downgraded.report["dropped_fields"])

    def test_missing_or_invalid_fields_are_version_errors(self) -> None:
        missing = make_config()
        missing.pop("chunk_size")
        missing_path = self.root / "missing.json"
        write_json(missing_path, missing)
        self.assert_error(E_VERSION_UNSUPPORTED, load_config, missing_path)

        invalid_mode = make_config()
        invalid_mode["compression"] = "not-a-codec"
        invalid_mode_path = self.root / "invalid-mode.json"
        write_json(invalid_mode_path, invalid_mode)
        self.assert_error(E_VERSION_UNSUPPORTED, load_config, invalid_mode_path)

        unsupported = make_config()
        unsupported["version"] = 9
        unsupported_path = self.root / "unsupported.json"
        write_json(unsupported_path, unsupported)
        self.assert_error(E_VERSION_UNSUPPORTED, load_config, unsupported_path)

        conflicting = make_config()
        conflicting["schema_version"] = 2
        conflicting_path = self.root / "conflicting.json"
        write_json(conflicting_path, conflicting)
        self.assert_error(E_VERSION_UNSUPPORTED, load_config, conflicting_path)

    def test_v2_metadata_and_shard_validation(self) -> None:
        result = upgrade_v1_to_v2(self.v1, self.root / "v2", shard_count=2)
        manifest = Path(result.target_path)
        shard = self.root / "v2" / "shards" / "shard_000.json"
        original = shard.read_text(encoding="utf-8")
        shard.write_text(original.replace('"a"', '"tampered"'), encoding="utf-8")
        self.assert_error(E_VERSION_UNSUPPORTED, load_config, manifest)
        shard.write_text(original, encoding="utf-8")

        database = self.root / "v2" / "metadata.sqlite"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "DELETE FROM config_meta WHERE key = 'chunk_size'"
            )
            connection.commit()
        finally:
            connection.close()
        self.assert_error(E_VERSION_UNSUPPORTED, load_config, manifest)

    def test_generation_floor_is_config_rollback(self) -> None:
        self.assert_error(
            E_CONFIG_ROLLBACK,
            load_config,
            self.v1,
            generation_floor=4,
        )

    def test_downgrade_flattens_unrepresentable_shard_clocks(self) -> None:
        result = upgrade_v1_to_v2(self.v1, self.root / "v2-clocks", shard_count=2)
        manifest = Path(result.target_path)
        database = self.root / "v2-clocks" / "metadata.sqlite"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE config_shards SET shard_clock = 200 WHERE shard_index = 1"
            )
            connection.commit()
        finally:
            connection.close()
        shard = self.root / "v2-clocks" / "shards" / "shard_001.json"
        shard_document = json.loads(shard.read_text(encoding="utf-8"))
        shard_document["shard_clock"] = 200
        shard.write_text(canonical_json(shard_document), encoding="utf-8")
        # The checksum now intentionally differs; update it so the fixture reaches
        # the flattening logic rather than the corruption check.
        checksum = sha256_text(canonical_json(shard_document))
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE config_shards SET checksum = ? WHERE shard_index = 1",
                (checksum,),
            )
            connection.commit()
        finally:
            connection.close()
        loaded = load_config(manifest)
        self.assertEqual(loaded.logical_clock, 200)
        downgraded = downgrade_v2_to_v1(manifest, self.root / "flattened.json")
        self.assertTrue(downgraded.report["flattened_fields"])
        self.assertEqual(load_config(downgraded.target_path).logical_clock, 200)


if __name__ == "__main__":
    unittest.main()
