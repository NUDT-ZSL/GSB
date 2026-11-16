from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from replication_lab.service import LabSession

try:
    from tests.helpers import write_legacy_segments
except ModuleNotFoundError:  # support `unittest discover -s tests` without -t
    from helpers import write_legacy_segments


OPS = [
    {
        "commit_seq": seq,
        "txn_id": 1000 + seq,
        "commit_offset_ms": seq * 100,
        "txn_size_bytes": seq * 64,
    }
    for seq in range(1, 9)
]


class SnapshotTests(unittest.TestCase):
    def test_snapshot_restore_replaces_whole_dirty_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with LabSession.open(root) as session:
                session.write_operations(OPS, records_per_segment=4)
                session.replay(replay_rate=10, concurrency=2, reset=True)
                snapshot = session.save_snapshot()
                snapshot_id = snapshot["snapshot_id"]
                before = session.verify_snapshot(snapshot_id)
                self.assertTrue(before["ok"])

                session.write_operations(
                    [
                        {
                            "commit_seq": 9,
                            "txn_id": 1009,
                            "commit_offset_ms": 900,
                            "txn_size_bytes": 512,
                        }
                    ],
                    records_per_segment=1,
                )
                dirty = session.verify_snapshot(snapshot_id)
                self.assertFalse(dirty["ok"])
                self.assertEqual(dirty["business_code"], "E_REPLAY")
                self.assertTrue(
                    any(item.startswith("segments.added:") for item in dirty["mismatch_items"])
                )
                comparison = session.compare_snapshot_with_disk(snapshot_id)
                self.assertFalse(comparison["view_matches"])
                restore = session.restore_snapshot(snapshot_id)
                self.assertEqual(restore["replace_mode"], "WHOLE_VIEW_REPLACED")
                self.assertTrue(restore["segments_replaced"])
                after = session.verify_snapshot(snapshot_id)
                self.assertTrue(after["ok"], after["mismatch_items"])
                self.assertEqual(session.version_set(), [3])
                state = session.store.get_state(session.session_id)
                self.assertEqual(state["sample_count"], 8)

    def test_parameter_override_reports_e_replay_stably(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with LabSession.open(Path(temp)) as session:
                session.write_operations(OPS, records_per_segment=4)
                session.replay(replay_rate=10, concurrency=2, reset=True)
                snapshot_id = session.save_snapshot()["snapshot_id"]
                result = session.verify_snapshot(snapshot_id, replay_rate=11)
                self.assertEqual(result["business_code"], "E_REPLAY")
                self.assertFalse(result["ok"])
                self.assertIn("replay_rate", result["mismatch_items"])
                self.assertEqual(result["stable_order"][2], "replay_rate")

    def test_restore_can_repair_a_corrupt_current_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with LabSession.open(root) as session:
                session.write_operations(OPS, records_per_segment=4)
                session.replay(replay_rate=10, concurrency=2, reset=True)
                snapshot_id = session.save_snapshot()["snapshot_id"]
            first_segment = sorted((root / "logs").glob("*.rlog"))[0]
            data = bytearray(first_segment.read_bytes())
            data[0:8] = b"BROKEN!!"
            first_segment.write_bytes(bytes(data))
            with LabSession.open(root) as session:
                self.assertTrue(session.format_errors)
                result = session.restore_snapshot(snapshot_id)
                self.assertTrue(result["segments_replaced"])
                self.assertFalse(session.format_errors)
                verification = session.verify_snapshot(snapshot_id)
                self.assertTrue(verification["ok"], verification["mismatch_items"])

    def test_snapshot_diff_identifies_versions_and_changed_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_legacy_segments(root / "logs")
            with LabSession.open(root, auto_upgrade=False) as legacy:
                legacy_id = legacy.save_snapshot()["snapshot_id"]
                self.assertEqual(legacy.version_set(), [1, 2, 3])
            with LabSession.open(root, auto_upgrade=True) as upgraded:
                self.assertEqual(upgraded.version_set(), [3])
                upgraded.replay(replay_rate=10, concurrency=2, reset=True)
                upgraded_id = upgraded.save_snapshot()["snapshot_id"]
                diff = upgraded.snapshot_diff(legacy_id, upgraded_id)
                self.assertTrue(diff["version_diff"]["changed"])
                self.assertEqual(diff["version_diff"]["left"], [1, 2, 3])
                self.assertEqual(diff["version_diff"]["right"], [3])
                self.assertTrue(diff["segment_diff"]["changed_segments"])
                self.assertTrue(diff["segment_diff"]["digest_changed_segments"])

    def test_restore_legacy_snapshot_restores_old_format_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_legacy_segments(root / "logs")
            with LabSession.open(root, auto_upgrade=False) as legacy:
                legacy_id = legacy.save_snapshot()["snapshot_id"]
            with LabSession.open(root, auto_upgrade=True) as session:
                self.assertEqual(session.version_set(), [3])
                result = session.restore_snapshot(legacy_id)
                self.assertTrue(result["segments_replaced"])
                self.assertEqual(result["restored_format_versions"], [1, 2, 3])
                self.assertEqual(session.version_set(), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
