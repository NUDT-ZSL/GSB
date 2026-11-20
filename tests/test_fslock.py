from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from schedkernel.fslock import FileLock, atomic_write_json


class FileLockTests(unittest.TestCase):
    def test_atomic_json_replace_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "status.json"
            atomic_write_json(target, {"version": 1, "ok": True})
            atomic_write_json(target, {"version": 2, "ok": True})
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"version": 2, "ok": True},
            )
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["status.json"],
            )

    def test_file_lock_can_be_reacquired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scheduler.lock"
            with FileLock(path, timeout_s=0.5):
                pass
            with FileLock(path, timeout_s=0.5):
                pass


if __name__ == "__main__":
    unittest.main()

