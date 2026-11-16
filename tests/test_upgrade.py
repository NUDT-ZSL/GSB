from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from replication_lab.segment import read_segment
from replication_lab.upgrade import UpgradeManager

try:
    from tests.helpers import mutate_u32, write_legacy_segments
except ModuleNotFoundError:  # support `unittest discover -s tests` without -t
    from helpers import mutate_u32, write_legacy_segments


class UpgradeTests(unittest.TestCase):
    def _manager(self, root: Path) -> UpgradeManager:
        return UpgradeManager(root / "logs", root / "backups", root / "checkpoints" / "upgrade.json")

    def test_upgrade_rewrites_all_records_and_keeps_readonly_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = write_legacy_segments(root / "logs")
            report = self._manager(root).run()
            self.assertEqual(report["version_set_before"], [1, 2, 3])
            self.assertEqual(report["version_set_after"], [3])
            self.assertFalse(report["failed"])
            self.assertEqual(len(report["upgraded"]), 2)
            self.assertEqual(len(report["skipped"]), 1)
            backups = sorted((root / "backups").glob("*.bak"))
            self.assertEqual(len(backups), 2)
            self.assertTrue(all((path.stat().st_mode & 0o222) == 0 for path in backups))
            for path in paths:
                segment = read_segment(path)
                self.assertEqual(segment.versions, (3,))
                for record in segment.records:
                    self.assertEqual(record.format_version, 3)
            first_upgraded = read_segment(paths[0])
            self.assertIsNone(first_upgraded.records[0].commit_ts_ms)

    def test_second_run_uses_checkpoint_and_does_not_duplicate_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_legacy_segments(root / "logs")
            manager = self._manager(root)
            manager.run()
            backup_names = sorted(path.name for path in (root / "backups").glob("*.bak"))
            second = manager.run()
            self.assertEqual(len(second["skipped"]), 3)
            self.assertFalse(second["upgraded"])
            self.assertEqual(
                sorted(path.name for path in (root / "backups").glob("*.bak")),
                backup_names,
            )

    def test_crash_after_replace_is_completed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_legacy_segments(root / "logs")
            manager = self._manager(root)

            def crash(_stage: str, filename: str) -> None:
                raise RuntimeError(f"simulated crash after {filename}")

            with self.assertRaises(RuntimeError):
                manager.run(crash_hook=crash)
            checkpoint = manager.load_checkpoint()
            first_name = "seg-00000001.rlog"
            self.assertEqual(checkpoint["segments"][first_name]["status"], "IN_PROGRESS")
            # The atomic replacement happened before the simulated crash.
            self.assertEqual(read_segment(root / "logs" / first_name).versions, (3,))

            restarted = manager.run()
            self.assertFalse(restarted["failed"])
            self.assertTrue(
                any(item.get("checkpoint_completed_after_crash") for item in restarted["skipped"])
            )
            self.assertEqual(restarted["version_set_after"], [3])
            checkpoint = manager.load_checkpoint()
            self.assertEqual(checkpoint["segments"][first_name]["status"], "COMPLETED")

    def test_invalid_segment_is_preserved_and_reported_e_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = write_legacy_segments(root / "logs")
            broken = paths[0]
            data = broken.read_bytes()
            corrupt = mutate_u32(data, 24, 0xFFFFFF)
            broken.write_bytes(corrupt)
            report = self._manager(root).run()
            self.assertEqual(report["business_code"], "E_FORMAT")
            self.assertTrue(report["failed"])
            self.assertEqual(report["failed"][0]["error"]["business_code"], "E_FORMAT")
            self.assertEqual(broken.read_bytes(), corrupt)
            checkpoint = self._manager(root).load_checkpoint()
            self.assertEqual(
                checkpoint["segments"][broken.name]["status"], "FAILED"
            )


if __name__ == "__main__":
    unittest.main()
