from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fusion_diag.config import (
    ConfigurationError,
    load_v1,
    load_v2,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
)

from .support import SampleWorkspace


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace = SampleWorkspace()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workspace.cleanup()

    def test_v1_and_v2_have_same_semantic_digest(self) -> None:
        v1 = load_v1(self.workspace.path("config_v1.json"))
        v2 = load_v2(self.workspace.path("config_v2"))
        self.assertEqual(v1.digest, v2.digest)
        self.assertEqual(v1.payload, v2.payload)
        self.assertEqual(v2.effective_at_ms, 1_700_000_000_000)
        self.assertTrue(v2.tombstones)

    def test_v1_to_v2_migration_records_each_completed_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "v2"
            report = migrate_v1_to_v2(
                self.workspace.path("config_v1.json"), destination
            )
            self.assertEqual(report["direction"], "v1_to_v2")
            self.assertEqual(
                [item["item"] for item in report["completed"]],
                ["effective_at_ms", "generation", "tombstones", "digest"],
            )
            loaded = load_v2(destination)
            self.assertEqual(
                loaded.digest,
                load_v1(self.workspace.path("config_v1.json")).digest,
            )

    def test_v2_to_v1_explicitly_drops_tombstones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "v1.json"
            report = migrate_v2_to_v1(
                self.workspace.path("config_v2"), destination
            )
            self.assertEqual(report["direction"], "v2_to_v1")
            self.assertTrue(report["dropped_tombstones"])
            self.assertEqual(
                report["drop_rule"], "not_representable_in_v1_explicit_drop"
            )
            self.assertTrue(destination.is_file())

    def test_tampered_shard_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "v2"
            shutil.copytree(self.workspace.path("config_v2"), target)
            shard = target / "shards" / "recall_lexical.json"
            value = json.loads(shard.read_text(encoding="utf-8"))
            value["value"]["limit"] += 1
            shard.write_text(
                json.dumps(value, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError) as caught:
                load_v2(target)
            self.assertEqual(caught.exception.field, "shards")


if __name__ == "__main__":
    unittest.main()

