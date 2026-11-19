from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from blockstore.errors import BusinessError
from blockstore.model import load_sample
from tests.helpers import ROOT, SAMPLE, run_cli_json


class ModelValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = json.loads(SAMPLE.read_text(encoding="utf-8"))

    def _parse_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.json"
            path.write_text(json.dumps(self.raw), encoding="utf-8")
            return load_sample(path)

    def test_fixed_sample_parses(self) -> None:
        sample = load_sample(SAMPLE)
        self.assertEqual(sample.volume.volume_id, "vol-demo")
        self.assertEqual(len(sample.snapshots), 6)
        self.assertEqual(len(sample.rollbacks), 3)

    def test_snapshot_array_order_does_not_define_parent_order(self) -> None:
        self.raw["snapshots"].reverse()
        sample = self._parse_modified()
        self.assertEqual(
            [snapshot.snapshot_id for snapshot in sample.snapshots],
            [
                "snap-001",
                "snap-002",
                "snap-003",
                "snap-004",
                "snap-005",
                "snap-006",
            ],
        )

    def test_digest_mismatch_is_rejected(self) -> None:
        self.raw["volume"]["blocks"][0]["digest"] = "0" * 64
        with self.assertRaises(BusinessError) as caught:
            self._parse_modified()
        self.assertEqual(caught.exception.code, "DIGEST_MISMATCH")

    def test_duplicate_snapshot_id_is_rejected(self) -> None:
        self.raw["snapshots"][1]["id"] = self.raw["snapshots"][0]["id"]
        with self.assertRaises(BusinessError) as caught:
            self._parse_modified()
        self.assertEqual(caught.exception.code, "DUPLICATE_SNAPSHOT_ID")

    def test_duplicate_snapshot_sequence_is_rejected(self) -> None:
        self.raw["snapshots"][1]["sequence"] = self.raw["snapshots"][0]["sequence"]
        with self.assertRaises(BusinessError) as caught:
            self._parse_modified()
        self.assertEqual(caught.exception.code, "DUPLICATE_SNAPSHOT_SEQUENCE")

    def test_parent_clock_must_be_strictly_earlier(self) -> None:
        self.raw["snapshots"][1]["clock_ms"] = self.raw["snapshots"][0]["clock_ms"]
        with self.assertRaises(BusinessError) as caught:
            self._parse_modified()
        self.assertEqual(caught.exception.code, "CLOCK_NOT_MONOTONIC")

    def test_range_must_match_declared_interval(self) -> None:
        self.raw["snapshots"][1]["ranges"][0]["end"] = 1
        with self.assertRaises(BusinessError) as caught:
            self._parse_modified()
        self.assertEqual(caught.exception.code, "INVALID_RANGE_COVERAGE")

    def test_out_of_bounds_range_is_rejected(self) -> None:
        self.raw["snapshots"][0]["ranges"][0]["start"] = 4
        self.raw["snapshots"][0]["ranges"][0]["end"] = 4
        self.raw["snapshots"][0]["ranges"][0]["blocks"][0]["block"] = 4
        with self.assertRaises(BusinessError) as caught:
            self._parse_modified()
        self.assertEqual(caught.exception.code, "BLOCK_RANGE_OUT_OF_BOUNDS")

    def test_invalid_load_does_not_create_state(self) -> None:
        invalid = copy.deepcopy(self.raw)
        invalid["volume"]["block_size"] = 0
        with tempfile.TemporaryDirectory() as directory:
            sample_path = Path(directory) / "invalid.json"
            state_path = Path(directory) / "state"
            sample_path.write_text(json.dumps(invalid), encoding="utf-8")
            result, payload = run_cli_json(
                state_path,
                "load",
                "--sample",
                str(sample_path),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNotNone(payload)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "INVALID_BLOCK_SIZE")
            self.assertFalse(state_path.exists())

    def test_readme_and_sample_exist(self) -> None:
        self.assertTrue((ROOT / "README.md").is_file())
        self.assertTrue(SAMPLE.is_file())
