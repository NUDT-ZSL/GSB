from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from layout_order.cli import main


class CliTests(unittest.TestCase):
    def test_generate_and_run_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            snapshots = root / "snapshots"
            report = root / "report.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "generate",
                        "--out",
                        str(data),
                        "--snapshot-dir",
                        str(snapshots),
                        "--pages",
                        "1",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue((data / "samples.jsonl").exists())
            self.assertTrue((snapshots / "current.json").exists())
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "run",
                        "--samples",
                        str(data / "samples.jsonl"),
                        "--config",
                        str(data / "dataset_config.json"),
                        "--cache-dir",
                        str(root / "cache"),
                        "--out",
                        str(report),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(report.exists())


if __name__ == "__main__":
    unittest.main()

