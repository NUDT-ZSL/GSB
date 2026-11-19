from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from intent_interop.config_io import parse_config_text, read_config_document
from intent_interop.errors import IntentError
from intent_interop.generator import generate_fixtures
from intent_interop.migration import (
    downgrade_v3_to_v2,
    migrate_document,
    upgrade_v1_to_v2,
    upgrade_v2_to_v3,
)
from intent_interop.writers import _finalize_text_config

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class ReaderMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not (DATA / "config_v3.json").exists():
            generate_fixtures(DATA)

    def test_three_readers(self) -> None:
        v1 = read_config_document(DATA / "config_v1.txt")
        v2 = read_config_document(DATA / "config_v2.txt")
        v3 = read_config_document(DATA / "config_v3.json")
        self.assertEqual(v1.source_version, "v1")
        self.assertEqual(v2.source_version, "v2")
        self.assertEqual(v3.source_version, "v3")
        self.assertEqual(
            [spec.intent for spec in v1.intents],
            [spec.intent for spec in v2.intents],
        )
        self.assertEqual(
            [spec.keywords for spec in v2.intents],
            [spec.keywords for spec in v3.intents],
        )
        self.assertEqual(v3.intents[0].label_path, ("refund_request",))
        self.assertEqual(v3.intents[0].weight_vector, (1.0, 1.0, 1.0, 1.0))

    def test_unknown_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "unknown.txt"
            path.write_text(
                "# version: v9\n# snapshot_version: 1\n",
                encoding="utf-8",
            )
            with self.assertRaises(IntentError) as raised:
                read_config_document(path)
        self.assertEqual(raised.exception.code, "E_VERSION_UNSUPPORTED")

    def test_bad_field_count_points_to_line(self) -> None:
        with self.assertRaises(IntentError) as raised:
            read_config_document(DATA / "config_v1_bad_fields.txt")
        self.assertEqual(raised.exception.code, "E_FORMAT_CORRUPT")
        self.assertEqual(raised.exception.details["line_no"], 6)
        self.assertEqual(raised.exception.details["actual"], 4)

    def test_missing_v2_key_points_to_line(self) -> None:
        with self.assertRaises(IntentError) as raised:
            read_config_document(DATA / "config_v2_missing_key.txt")
        self.assertEqual(raised.exception.code, "E_FORMAT_CORRUPT")
        self.assertEqual(raised.exception.details["line_no"], 6)
        self.assertIn("fallback_intent", raised.exception.details["missing_keys"])

    def test_duplicate_intent_points_to_line_and_label(self) -> None:
        labels = "a,b,fallback_unknown"
        text = _finalize_text_config(
            [
                "# version: v1",
                "# snapshot_version: 1",
                f"# controlled_labels: {labels}",
                "# fallback_intent: fallback_unknown",
            ],
            ["a|词一|1", "a|词二|2"],
        )
        with self.assertRaises(IntentError) as raised:
            parse_config_text(text)
        self.assertEqual(raised.exception.code, "E_DUP_INTENT")
        self.assertEqual(raised.exception.details["intent"], "a")
        self.assertEqual(raised.exception.details["line_no"], 7)

    def test_checksum_tamper_is_snapshot_corrupt(self) -> None:
        with self.assertRaises(IntentError) as raised:
            read_config_document(DATA / "config_v2_corrupt.txt")
        self.assertEqual(raised.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(raised.exception.details["version"], 2)
        self.assertGreaterEqual(raised.exception.details["field_offset"], 0)

    def test_upgrade_fills_defaults_and_keeps_match_fields(self) -> None:
        v1 = read_config_document(DATA / "config_v1.txt")
        to_v2 = upgrade_v1_to_v2(v1)
        self.assertEqual(to_v2.document.source_version, "v2")
        self.assertTrue(
            all(spec.threshold == 0.1 for spec in to_v2.document.intents)
        )
        self.assertTrue(
            all(
                spec.fallback_intent == "fallback_unknown"
                for spec in to_v2.document.intents
            )
        )
        self.assertEqual(
            [spec.intent for spec in v1.intents],
            [spec.intent for spec in to_v2.document.intents],
        )
        self.assertEqual(
            [spec.keywords for spec in v1.intents],
            [spec.keywords for spec in to_v2.document.intents],
        )
        self.assertTrue(any(row["field"] == "threshold" for row in to_v2.actions))

        to_v3 = upgrade_v2_to_v3(to_v2.document)
        self.assertEqual(to_v3.document.source_version, "v3")
        self.assertTrue(
            all(spec.label_path == (spec.intent,) for spec in to_v3.document.intents)
        )
        self.assertTrue(
            all(
                spec.weight_vector == (1.0,) * len(spec.keywords)
                for spec in to_v3.document.intents
            )
        )

    def test_v3_downgrade_reports_drop_and_flatten(self) -> None:
        rich = read_config_document(DATA / "config_v3_rich.json")
        dropped = downgrade_v3_to_v2(rich, hierarchy_policy="drop")
        self.assertTrue(dropped.semantic_loss)
        self.assertIn("label_path", dropped.losses)
        self.assertIn("slots", dropped.losses)
        self.assertIn("weight_vector", dropped.losses)
        self.assertEqual(
            dropped.document.intents[0].intent,
            "refund_request",
        )

        flattened = downgrade_v3_to_v2(rich, hierarchy_policy="flatten")
        self.assertEqual(
            flattened.document.intents[0].intent,
            "service/after_sale/refund_request",
        )
        flatten_actions = [
            row for row in flattened.actions if row["action"] == "flatten"
        ]
        self.assertEqual(len(flatten_actions), 1)
        self.assertFalse(flatten_actions[0]["matching_semantics_unchanged"])

    def test_v3_downgrade_reject(self) -> None:
        rich = read_config_document(DATA / "config_v3_rich.json")
        with self.assertRaises(IntentError) as raised:
            downgrade_v3_to_v2(
                rich,
                hierarchy_policy="drop",
                loss_policy="reject",
            )
        self.assertEqual(raised.exception.code, "E_SEMANTIC_LOSS")
        self.assertIn("weight_vector", raised.exception.details["fields"])

    def test_bidirectional_round_trip_preserves_labels_and_keywords(self) -> None:
        v2 = read_config_document(DATA / "config_v2.txt")
        down = migrate_document(v2, "v1")
        up = migrate_document(down.document, "v2")
        self.assertEqual(
            [(spec.intent, spec.keywords) for spec in v2.intents],
            [(spec.intent, spec.keywords) for spec in up.document.intents],
        )


if __name__ == "__main__":
    unittest.main()
