from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from seqledger.merge import merge_snapshots
from seqledger.sample import generate_fixtures
from seqledger.util import read_json
from seqledger.validation import validate_v1, validate_v2


class MergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = TemporaryDirectory()
        cls.root = Path(cls.tempdir.name) / "fixtures"
        generate_fixtures(cls.root)
        cls.v1 = validate_v1(read_json(cls.root / "semantics_v1.json"))
        cls.v2 = validate_v2(
            read_json(cls.root / "semantics_v2" / "manifest.json"),
            str(cls.root / "semantics_v2" / "manifest.json"),
            meta_path=cls.root / "semantics_v2" / "meta.sqlite3",
        )
        cls.view = merge_snapshots(
            cls.v1,
            cls.v2,
            injected_logical_clock_ms=3000,
        )
        cls.entries = {
            entry["message_key"]: entry for entry in cls.view["entries"]
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    def test_every_entry_preserves_source_and_original_time(self) -> None:
        for entry in self.view["entries"]:
            self.assertIn(entry["source_version"], {"v1", "v2"})
            self.assertIn("original_time", entry)
            self.assertIn("arbitration", entry)
            self.assertTrue(entry["source_origins"])
            self.assertIn("basis", entry["arbitration"])

    def test_v1_conversion_records_scale_rounding_and_remainder(self) -> None:
        conversion = self.view["conversions"][0]["conversion"]
        self.assertEqual(conversion["scale_ms_per_second"], 1000)
        self.assertEqual(conversion["rounding"], "floor")
        self.assertEqual(conversion["discarded_remainder_ms"], 0)
        self.assertEqual(
            conversion["absolute_effective_ms"],
            3000 + self.view["conversions"][0]["original_time"]["value"] * 1000,
        )

    def test_v2_wins_overlap_and_keeps_loser_evidence(self) -> None:
        entry = self.entries["ord-1002"]
        self.assertEqual(entry["source_version"], "v2")
        self.assertEqual(entry["arbitration"]["winner"]["logical_clock_ms"], 1800)
        overridden_sources = {
            candidate["source_version"]
            for candidate in entry["arbitration"]["overridden"]
        }
        self.assertEqual(overridden_sources, {"v1"})
        loser = entry["arbitration"]["overridden"][0]
        self.assertEqual(loser["effective_ms"], 4000)

    def test_v1_only_entries_use_converted_absolute_time(self) -> None:
        entry = self.entries["ord-1003"]
        self.assertEqual(entry["source_version"], "v1")
        self.assertEqual(entry["effective_ms"], 5000)
        self.assertEqual(entry["conversion"]["kind"], "relative-seconds-to-absolute-ms")

    def test_v2_dependencies_and_tombstone_are_carried_into_view(self) -> None:
        self.assertIsNotNone(self.entries["pay-AB-002"]["dependency"])
        self.assertEqual(
            self.entries["ord-1005"]["tombstone"]["tombstone_id"],
            "tomb-orders-1005",
        )


if __name__ == "__main__":
    unittest.main()
