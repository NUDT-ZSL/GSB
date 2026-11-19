from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT


class CliSmokeTests(unittest.TestCase):
    def test_demo_runs_non_interactively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "snapshots.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "app.py"),
                    "--snapshot-path",
                    str(snapshot),
                    "demo",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("CONSISTENT", completed.stdout)
        self.assertIn("E_SEMANTIC_GAP", completed.stdout)
        self.assertIn("PASS", completed.stdout)
        self.assertIn("ok                      : True", completed.stdout)

    def test_menu_exits_cleanly_on_eof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "app.py"),
                    "--snapshot-path",
                    str(Path(temporary) / "snapshots.json"),
                    "menu",
                ],
                cwd=ROOT,
                input="",
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("交互菜单", completed.stdout)


if __name__ == "__main__":
    unittest.main()
