from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from replication_lab.demo import run_demo


class DemoAcceptanceTests(unittest.TestCase):
    def test_three_scenarios_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = run_demo(Path(temp) / "demo")
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["scenarios"]), 3)
            self.assertEqual(result["scenarios"][0]["format_version_set"], [3])
            self.assertEqual(result["scenarios"][0]["sample_count"], 8)
            self.assertEqual(result["scenarios"][1]["legacy_format_version_set"], [1, 2, 3])
            self.assertEqual(result["scenarios"][1]["unknown_delay_count"], 2)
            self.assertEqual(result["scenarios"][2]["recovery"]["unknown_batch_count"], 1)
            self.assertEqual(result["scenarios"][2]["dirty_view_business_code"], "E_REPLAY")
            self.assertTrue((Path(temp) / "demo" / "demo_report.json").exists())
            self.assertTrue((Path(temp) / "demo" / "demo_report.md").exists())
            self.assertTrue(
                all(result["acceptance"].values()), result["acceptance"]
            )


if __name__ == "__main__":
    unittest.main()

