from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from meeting_post.config import default_config
from meeting_post.errors import MeetingError
from meeting_post.pipeline import assert_result_fresh, process_config
from meeting_post.snapshots import SnapshotStore


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_whole_payload_replacement_and_monotonic_version(self) -> None:
        first = self.store.commit("segmentation", {"threshold_multiplier": 1.8})
        second = self.store.commit("segmentation", {"threshold_multiplier": 2.0})
        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)
        reloaded = self.store.read("segmentation")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.payload, {"threshold_multiplier": 2.0})
        self.assertNotEqual(first.checksum, second.checksum)

    def test_rollback_is_blocked(self) -> None:
        self.store.commit("reid", {"threshold": 0.55}, version=3)
        with self.assertRaises(MeetingError) as context:
            self.store.commit("reid", {"threshold": 0.40}, version=2)
        error = context.exception
        self.assertEqual(error.code, "E_SNAPSHOT_ROLLBACK")
        self.assertEqual(error.details["item"], "reid")
        self.assertEqual(error.details["current_version"], 3)
        self.assertEqual(error.details["attempted_version"], 2)

    def test_checksum_corruption_names_item_and_version(self) -> None:
        self.store.commit("separation", {"mask_mode": "binary"}, version=4)
        path = self.store.path_for("separation")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["payload"]["mask_mode"] = "weighted"
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaises(MeetingError) as context:
            self.store.read("separation")
        error = context.exception
        self.assertEqual(error.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(error.details["item"], "separation")
        self.assertEqual(error.details["version"], 4)

    def test_same_version_content_change_is_corrupt(self) -> None:
        self.store.commit("reid", {"threshold": 0.55}, version=1)
        with self.assertRaises(MeetingError) as context:
            self.store.commit("reid", {"threshold": 0.60}, version=1)
        self.assertEqual(context.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(context.exception.details["version"], 1)

    def test_snapshot_version_change_makes_existing_result_stale(self) -> None:
        config = default_config("v3")
        self.store.update_from_config(config)
        result = process_config(config, self.store)
        assert_result_fresh(result, self.store)
        self.store.update_from_config(config, force_version=True)
        with self.assertRaises(MeetingError) as context:
            assert_result_fresh(result, self.store)
        self.assertEqual(context.exception.code, "E_RESULT_STALE")
        replacement = process_config(config, self.store)
        self.assertNotEqual(result.snapshot_digest, replacement.snapshot_digest)


if __name__ == "__main__":
    unittest.main()
