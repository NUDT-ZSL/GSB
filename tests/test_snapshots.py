from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from latentflow.errors import BusinessError, ErrorCode
from latentflow.snapshot import SnapshotStore


class SnapshotTests(unittest.TestCase):
    def test_monotonic_versions_checksums_and_whole_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory))
            first = store.commit("ready_blocks", {"blocks": [0, 1, 2]})
            second = store.commit("ready_blocks", {"blocks": [3]})
            plan = store.commit("chunk_plan", {"block_size": 4})

            self.assertEqual(first.version, 1)
            self.assertEqual(second.version, 2)
            self.assertEqual(plan.version, 1)
            loaded = store.load("ready_blocks")
            self.assertEqual(loaded.version, 2)
            self.assertEqual(loaded.payload, {"blocks": [3]})

            restarted = SnapshotStore(Path(directory))
            observation = restarted.observe_replacement("ready_blocks")
            self.assertTrue(observation["replaced"])
            loaded_after_restart = restarted.load_all()
            self.assertEqual(loaded_after_restart["ready_blocks"].version, 2)
            self.assertEqual(loaded_after_restart["ready_blocks"].payload, {"blocks": [3]})
            next_snapshot = restarted.commit("ready_blocks", {"blocks": []})
            self.assertEqual(next_snapshot.version, 3)

    def test_corrupt_checksum_identifies_category_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory))
            snapshot = store.commit("decode_params", {"steps": 4, "seed": 7})
            active = Path(directory) / "active-decode_params.json"
            parsed = json.loads(active.read_text("utf-8"))
            parsed["payload"]["seed"] = 8
            active.write_text(json.dumps(parsed), encoding="utf-8")

            with self.assertRaises(BusinessError) as captured:
                store.load("decode_params")
            error = captured.exception
            self.assertEqual(error.code, ErrorCode.E_SNAPSHOT_CORRUPT)
            self.assertEqual(error.details["category"], "decode_params")
            self.assertEqual(error.details["version"], snapshot.version)

    def test_load_all_raises_corruption_through_verification_api_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory))
            store.commit("chunk_plan", {"block_size": 4})
            active = Path(directory) / "active-chunk_plan.json"
            parsed = json.loads(active.read_text("utf-8"))
            parsed["payload"]["block_size"] = 2
            active.write_text(json.dumps(parsed), encoding="utf-8")

            with self.assertRaises(BusinessError) as captured:
                store.load_all()
            self.assertEqual(captured.exception.code, ErrorCode.E_SNAPSHOT_CORRUPT)
            self.assertEqual(captured.exception.details["category"], "chunk_plan")


if __name__ == "__main__":
    unittest.main()
