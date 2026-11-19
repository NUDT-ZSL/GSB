from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from intent_interop.errors import IntentError
from intent_interop.injection import FaultInjector, parse_injection_specs
from intent_interop.pipeline import injection_run
from intent_interop.snapshots import SnapshotStore

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class SnapshotInjectionTests(unittest.TestCase):
    def test_version_bump_replaces_whole_snapshot(self) -> None:
        store = SnapshotStore()
        first = store.load_file(DATA / "config_v2.txt", namespace="n")
        second = store.load_file(
            DATA / "config_v2_snapshot2_retired.txt",
            namespace="n",
        )
        self.assertTrue(first.replaced)
        self.assertTrue(second.replaced)
        self.assertEqual(second.snapshot.generation, 2)
        self.assertEqual(second.reason, "version_changed")
        self.assertEqual(second.snapshot.version, 2)
        self.assertIn("refund_request", second.diff["removed_labels"])
        self.assertNotIn("refund_request", second.snapshot.document.controlled_labels)

    def test_rollback_is_rejected_and_state_is_unchanged(self) -> None:
        store = SnapshotStore()
        store.load_file(DATA / "config_v2.txt", namespace="n")
        replacement = store.load_file(
            DATA / "config_v2_snapshot2_retired.txt",
            namespace="n",
        )
        with self.assertRaises(IntentError) as raised:
            store.load_file(DATA / "config_v2.txt", namespace="n")
        self.assertEqual(raised.exception.code, "E_SNAPSHOT_ROLLBACK")
        self.assertEqual(store.current("n").checksum, replacement.snapshot.checksum)
        self.assertEqual(store.current("n").version, 2)

    def test_same_version_new_checksum_replaces_snapshot(self) -> None:
        store = SnapshotStore()
        store.load_file(DATA / "config_v2.txt", namespace="n")
        replacement = store.load_file(
            DATA / "config_v2_same_version_replaced.txt",
            namespace="n",
        )
        self.assertTrue(replacement.replaced)
        self.assertEqual(replacement.reason, "checksum_changed_same_version")
        self.assertEqual(replacement.snapshot.generation, 2)
        self.assertEqual(replacement.snapshot.version, 1)

    def test_corrupt_checksum_reports_version_and_offset(self) -> None:
        store = SnapshotStore()
        with self.assertRaises(IntentError) as raised:
            store.load_file(DATA / "config_v2_corrupt.txt")
        self.assertEqual(raised.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(raised.exception.details["version"], 2)
        self.assertIsInstance(raised.exception.details["field_offset"], int)

    def test_structural_damage_is_snapshot_corrupt_in_store(self) -> None:
        store = SnapshotStore()
        with self.assertRaises(IntentError) as raised:
            store.load_file(DATA / "config_v1_bad_fields.txt")
        self.assertEqual(raised.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(raised.exception.details["cause_code"], "E_FORMAT_CORRUPT")
        self.assertGreater(raised.exception.details["field_offset"], 0)

    def test_three_stage_injection_records(self) -> None:
        cases = (
            ("read:field_count", "E_FORMAT_CORRUPT", "read", "line_no"),
            ("migrate:missing_key", "E_FORMAT_CORRUPT", "migrate", "line_no"),
            ("match:empty_keyword", "E_CONFIG_INVALID", "match", "query_id"),
            ("match:offset_length", "E_INCONSISTENT", "match", "query_id"),
        )
        for expression, code, stage, locator in cases:
            with self.subTest(expression=expression):
                report, exit_code = injection_run(
                    samples_path=DATA / "queries.jsonl",
                    config_path=DATA / "config_v1.txt",
                    injector=FaultInjector(parse_injection_specs([expression])),
                )
                self.assertEqual(exit_code, 2)
                self.assertEqual(report["status"], code)
                self.assertEqual(len(report["attributed_errors"]), 1)
                record = report["attributed_errors"][0]
                self.assertEqual(record["stage"], stage)
                self.assertEqual(record["code"], code)
                self.assertIn(locator, record)
                self.assertEqual(report["unattributed_errors"], [])

    def test_unattributed_injection_is_separate(self) -> None:
        report, exit_code = injection_run(
            samples_path=DATA / "queries.jsonl",
            config_path=DATA / "config_v1.txt",
            injector=FaultInjector(parse_injection_specs(["match:unexpected"])),
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "E_UNATTRIBUTED")
        self.assertEqual(report["attributed_errors"], [])
        self.assertEqual(len(report["unattributed_errors"]), 1)
        self.assertFalse(report["unattributed_errors"][0]["attributed"])


if __name__ == "__main__":
    unittest.main()
