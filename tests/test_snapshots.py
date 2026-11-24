from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from anim_replay.canonical import digest_of_body
from anim_replay.snapshots import merge_snapshots

from tests.helpers import sample_root


class SnapshotMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = sample_root()
        cls.v1 = cls.root / "snapshots" / "v1.json"
        cls.v2 = cls.root / "snapshots" / "v2_valid"

    def test_valid_merge_uses_generation_tombstone_and_time(self) -> None:
        result = merge_snapshots(v1_path=self.v1, v2_dir=self.v2, bucket_ms=16)
        self.assertTrue(result["ok"])
        tombstone = [
            record
            for record in result["records"]
            if record["canonical_clip_id"] == "anim://clip/walk" and record["tombstone"]
        ]
        self.assertTrue(tombstone)
        self.assertEqual(tombstone[0]["logical_ms"], 432)
        conflict = next(
            item
            for item in result["conflicts"]
            if "g1_state_generation_conflict" in item["winner"]["path"]
        )
        self.assertEqual(conflict["resolution"], "state_generation")
        self.assertTrue(conflict["differing_channels"])
        self.assertTrue(conflict["differing_channels"][0]["bone_channel"])
        self.assertTrue(all(item["winner"]["path"] for item in result["conflicts"]))

    def test_tampered_v2_returns_digest_and_falls_back_generation(self) -> None:
        result = merge_snapshots(
            v1_path=self.v1,
            v2_dir=self.root / "snapshots" / "v2_tampered",
            bucket_ms=16,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "E_DIGEST")
        self.assertEqual(result["fallback"]["generation"], 2)
        self.assertEqual(result["snapshots"]["v2"]["valid_generations"], [1, 2])
        self.assertEqual(result["snapshots"]["v2"]["invalid_generations"], [3])

    def test_tampered_v1_returns_digest_and_uses_v2(self) -> None:
        result = merge_snapshots(
            v1_path=self.root / "snapshots" / "v1_tampered.json",
            v2_dir=self.v2,
            bucket_ms=16,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "E_DIGEST")
        self.assertFalse(result["fallback"]["v1_valid"])
        self.assertEqual(result["fallback"]["generation"], 2)

    def test_missing_digest_and_frame_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            missing = directory / "missing_digest.json"
            body = json.loads(self.v1.read_text(encoding="utf-8"))
            body.pop("content_digest")
            missing.write_text(json.dumps(body), encoding="utf-8")
            result = merge_snapshots(v1_path=missing, v2_dir=self.v2, bucket_ms=16)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "E_DIGEST")

            count_bad = directory / "count_bad.json"
            body = json.loads(self.v1.read_text(encoding="utf-8"))
            body["frame_count"] = int(body["frame_count"]) + 1
            body["content_digest"] = digest_of_body(body)
            count_bad.write_text(json.dumps(body), encoding="utf-8")
            result = merge_snapshots(v1_path=count_bad, v2_dir=self.v2, bucket_ms=16)
            self.assertFalse(result["ok"])
            reasons = {item["reason"] for item in result["digest_issues"]}
            self.assertIn("frame_count_mismatch", reasons)

    def test_merge_output_is_deterministic(self) -> None:
        left = merge_snapshots(v1_path=self.v1, v2_dir=self.v2, bucket_ms=16)
        right = merge_snapshots(v1_path=self.v1, v2_dir=self.v2, bucket_ms=16)
        self.assertEqual(json.dumps(left, sort_keys=True), json.dumps(right, sort_keys=True))


if __name__ == "__main__":
    unittest.main()

