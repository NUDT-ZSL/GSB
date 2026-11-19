from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from annotation_app.storage import atomic_write_json, cleanup_stale_temps, read_json

from tests.helpers import ROOT


CHILD = r"""
import json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
target = root / "state.json"
tmp = root / ".state.json.child.tmp"
with tmp.open("wb") as handle:
    handle.write(b'{"partial": true')
    handle.flush()
    os.fsync(handle.fileno())
(root / "ready.signal").write_text("ready", encoding="utf-8")
time.sleep(30)
os.replace(tmp, target)
"""


class RecoveryTests(unittest.TestCase):
    def test_forced_kill_leaves_previous_active_file_intact(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            path = directory / "state.json"
            atomic_write_json(path, {"generation": 1, "status": "complete"})
            child = subprocess.Popen(
                [sys.executable, "-c", CHILD, str(directory)],
                cwd=str(ROOT),
            )
            try:
                signal_path = directory / "ready.signal"
                deadline = time.monotonic() + 10
                while not signal_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(signal_path.exists(), "child did not reach partial write")
                child.kill()
                child.wait(timeout=10)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
            self.assertEqual(read_json(path), {"generation": 1, "status": "complete"})
            self.assertTrue((directory / ".state.json.child.tmp").exists())
            removed = cleanup_stale_temps(directory)
            self.assertIn(".state.json.child.tmp", removed)
            self.assertFalse((directory / ".state.json.child.tmp").exists())


if __name__ == "__main__":
    unittest.main()

