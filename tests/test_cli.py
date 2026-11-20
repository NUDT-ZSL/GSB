import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_demo_cli_writes_complete_report(self):
        with tempfile.TemporaryDirectory() as tempdir:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "aed_scale",
                    "demo",
                    "--out",
                    tempdir,
                    "--seed",
                    "777",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["business_code"], "OK")
            report_path = Path(tempdir) / "report.json"
            self.assertTrue(report_path.exists())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["small"]["selected_path"], "fine")
            self.assertEqual(report["summary"]["large"]["selected_path"], "approximate")
            self.assertTrue(report["summary"]["large"]["degraded"])
            self.assertGreater(
                report["summary"]["small_fine_vs_approximate"]["event_recall_delta"],
                0,
            )
            for run in report["runs"].values():
                self.assertEqual(run["business_code"], "OK")
                self.assertEqual(run["consistency"]["status"], "OK")


if __name__ == "__main__":
    unittest.main()
