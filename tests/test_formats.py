from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dialog_state_engine.canonical import atomic_write_json, read_json
from dialog_state_engine.config import Rules
from dialog_state_engine.errors import (
    E_FORMAT_CORRUPT,
    E_SEMANTIC_DRIFT,
    E_VERSION_UNSUPPORTED,
    EngineError,
)
from dialog_state_engine.formats import read_state
from dialog_state_engine.migration import migrate
from dialog_state_engine.samples import generate_samples
from dialog_state_engine.turns import load_turns


class FormatAndMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory(prefix="dialog-format-tests-")
        cls.sample_dir = Path(cls._temp.name) / "samples"
        cls.manifest = generate_samples(cls.sample_dir)
        cls.rules = Rules.from_file(cls.sample_dir / "config.json")
        cls.paths = {
            "v1": cls.sample_dir / "state_v1.json",
            "v2": cls.sample_dir / "state_v2",
            "v3": cls.sample_dir / "state_v3.json",
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def test_three_formats_have_identical_semantic_state(self) -> None:
        results = {
            version: read_state(path, self.rules)
            for version, path in self.paths.items()
        }
        checksums = {
            version: result.state.state_checksum
            for version, result in results.items()
        }
        self.assertEqual(len(set(checksums.values())), 1)
        destination = {
            version: result.state.slot_map["destination"]
            for version, result in results.items()
        }
        for version, slot in destination.items():
            self.assertEqual(slot.winner.value, "上海", version)
            self.assertTrue(slot.conflict, version)
            self.assertEqual(
                {candidate.value for candidate in slot.overwritten},
                {"杭州", "北京"},
                version,
            )
            self.assertNotIn(
                slot.winner.candidate_key,
                {candidate.candidate_key for candidate in slot.overwritten},
            )
        self.assertEqual(
            results["v1"].state.pending_clarifications,
            ("destination", "guests", "meal_preference", "transport"),
        )
        self.assertEqual(len(results["v3"].audit_history), 7)
        self.assertEqual(len(results["v1"].audit_history), 0)

    def test_v1_upgrade_records_each_default_source(self) -> None:
        result = read_state(self.paths["v1"], self.rules)
        fields = {item["field"] for item in result.completions}
        self.assertIn("state_generation", fields)
        self.assertIn("updated_at_ms", fields)
        self.assertIn("change_history", fields)
        self.assertIn("state_checksum", fields)
        self.assertEqual(
            result.state.state_generation,
            self.rules.default_state_generation,
        )

    def test_all_format_pairs_migrate_with_semantic_equality(self) -> None:
        baseline = read_state(self.paths["v3"], self.rules).state
        with tempfile.TemporaryDirectory(prefix="dialog-migrations-") as temp:
            temp_path = Path(temp)
            for source_version, source_path in self.paths.items():
                for target_version in ("v1", "v2", "v3"):
                    if source_version == target_version:
                        continue
                    target = temp_path / f"{source_version}-to-{target_version}"
                    if target_version == "v2":
                        target = target.with_suffix(".dir")
                    result = migrate(
                        source_path,
                        target,
                        target_version=target_version,
                        rules=self.rules,
                        overwrite=True,
                    )
                    self.assertTrue(result.semantic_equal)
                    self.assertEqual(
                        result.state.to_semantic_dict(),
                        baseline.to_semantic_dict(),
                    )
                    if source_version == "v3" and target_version in ("v1", "v2"):
                        self.assertEqual(
                            result.drops[0]["action"],
                            "drop",
                        )
                        self.assertEqual(result.drops[0]["event_count"], 7)
                    else:
                        self.assertEqual(result.drops, ())

    def test_strict_nested_history_drop_returns_semantic_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dialog-strict-") as temp:
            with self.assertRaises(EngineError) as caught:
                migrate(
                    self.paths["v3"],
                    Path(temp) / "state.json",
                    target_version="v1",
                    rules=self.rules,
                    strict_audit_drops=True,
                )
        self.assertEqual(caught.exception.code, E_SEMANTIC_DRIFT)
        self.assertEqual(caught.exception.field, "change_history")

    def test_unknown_version_is_unsupported(self) -> None:
        data = read_json(self.paths["v3"])
        data["format"] = "slot-state-v9"
        with tempfile.TemporaryDirectory(prefix="dialog-unknown-") as temp:
            path = Path(temp) / "state.json"
            atomic_write_json(path, data)
            with self.assertRaises(EngineError) as caught:
                read_state(path, self.rules)
        self.assertEqual(caught.exception.code, E_VERSION_UNSUPPORTED)
        self.assertEqual(caught.exception.field, "format")

    def test_v3_checksum_corruption_is_rejected(self) -> None:
        data = read_json(self.paths["v3"])
        first_slot = next(iter(data["slots"].values()))
        first_slot["winner"]["confidence"] = 0.123
        with tempfile.TemporaryDirectory(prefix="dialog-checksum-") as temp:
            path = Path(temp) / "state.json"
            atomic_write_json(path, data)
            with self.assertRaises(EngineError) as caught:
                read_state(path, self.rules)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)
        self.assertEqual(caught.exception.field, "checksum")

    def test_v1_missing_required_field_is_corrupt(self) -> None:
        data = read_json(self.paths["v1"])
        del data["session_id"]
        with tempfile.TemporaryDirectory(prefix="dialog-v1-corrupt-") as temp:
            path = Path(temp) / "state.json"
            atomic_write_json(path, data)
            with self.assertRaises(EngineError) as caught:
                read_state(path, self.rules)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)
        self.assertEqual(caught.exception.field, "session_id")

    def test_v2_sqlite_generation_disagreement_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dialog-v2-corrupt-") as temp:
            copied = Path(temp) / "state_v2"
            shutil.copytree(self.paths["v2"], copied)
            connection = sqlite3.connect(copied / "state.sqlite")
            try:
                connection.execute(
                    "UPDATE meta SET value = '999' WHERE key = 'state_generation'"
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(EngineError) as caught:
                read_state(copied, self.rules)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)
        self.assertIn("sqlite.meta", caught.exception.field)

    def test_jsonl_field_count_mismatch_is_corrupt(self) -> None:
        rows = [
            json.loads(line)
            for line in (self.sample_dir / "turns.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        rows[0]["unexpected"] = True
        with tempfile.TemporaryDirectory(prefix="dialog-jsonl-") as temp:
            path = Path(temp) / "turns.jsonl"
            path.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(EngineError) as caught:
                load_turns(path, self.rules)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)
        self.assertEqual(caught.exception.field, "line:1.fields")
        self.assertEqual(caught.exception.details["actual_count"], 6)


if __name__ == "__main__":
    unittest.main()

