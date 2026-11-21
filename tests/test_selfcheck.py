from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from layout_order.selfcheck import run_self_check


class SelfCheckTests(unittest.TestCase):
    def test_self_check_passes_and_reports_reproducible_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = run_self_check(
                root / "report.json",
                workdir=root,
                page_count=2,
            )
            self.assertTrue(report["passed"])
            self.assertTrue(
                any(
                    check["name"] == "fixed_seed_report_hash_reproducible"
                    and check["passed"]
                    for check in report["checks"]
                )
            )
            self.assertIn("E_SNAPSHOT_ROLLBACK", report["business_codes"])
            self.assertIn("E_STAGE_BUSY", report["business_codes"])
            self.assertIn("E_SEMANTIC_DRIFT", report["business_codes"])


if __name__ == "__main__":
    unittest.main()

