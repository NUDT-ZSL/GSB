from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from flight_compile_cache.cli import main


class CliTests(unittest.TestCase):
    def test_generate_validate_run_edge_and_snapshot_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            run = root / "run"
            edge = root / "edge"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                self.assertEqual(main(["generate", "--out-dir", str(generated)]), 0)
                self.assertEqual(
                    main(
                        [
                            "validate",
                            "--project",
                            str(generated / "project.json"),
                            "--out",
                            str(generated / "validation.json"),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "run",
                            "--project",
                            str(generated / "project.json"),
                            "--out-dir",
                            str(run),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "edge",
                            "--project",
                            str(generated / "edge_project.json"),
                            "--out-dir",
                            str(edge),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "snapshot-check",
                            "--project",
                            str(generated / "project.json"),
                            "--snapshot",
                            str(run / "cache_checkpoint.json"),
                        ]
                    ),
                    0,
                )
            report = json.loads((run / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["overall_ok"])
            self.assertTrue(report["restart_verification"]["hit_decisions_identical"])
            edge_report = json.loads((edge / "edge_report.json").read_text(encoding="utf-8"))
            self.assertTrue(edge_report["all_expected_outcomes_match"])


if __name__ == "__main__":
    unittest.main()
