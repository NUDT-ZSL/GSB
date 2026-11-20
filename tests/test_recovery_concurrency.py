from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.concurrency_check import run_check as run_concurrency
from scripts.crash_recovery_check import run_check as run_crash_recovery


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def test_crash_at_all_stages(self) -> None:
        with TemporaryDirectory() as temp:
            report = run_crash_recovery(Path(temp) / "crash")
            self.assertTrue(report["ok"])
            outcomes = {case["crash_at"]: case for case in report["cases"]}
            self.assertEqual(
                outcomes["after_prepare"]["after_resume"]["prepared_statuses"],
                {"CLEANED": 1},
            )
            self.assertEqual(outcomes["after_prepare"]["after_resume"]["leader"], None)
            self.assertEqual(
                outcomes["after_final_insert"]["after_resume"]["prepared_statuses"],
                {"CLEANED": 1},
            )
            self.assertEqual(outcomes["after_commit"]["after_resume"]["leader"], "bravo")
            self.assertEqual(
                outcomes["after_commit"]["after_resume"]["prepared_statuses"],
                {"COMMITTED": 1},
            )

    def test_multiprocess_single_grant_and_renewal(self) -> None:
        with TemporaryDirectory() as temp:
            report = run_concurrency(Path(temp) / "concurrent", workers=6)
            self.assertTrue(report["ok"], report)
            self.assertEqual(
                report["concurrent_bootstrap"]["unique_election_ids"],
                [report["concurrent_bootstrap"]["unique_election_ids"][0]],
            )
            self.assertEqual(report["concurrent_bootstrap"]["winner_values"], ["bravo"])
            self.assertEqual(report["leases"]["current_rows"], 3)
            self.assertEqual(report["leases"]["history_rows"], 3)
            self.assertEqual(report["leases"]["generation_duplicates"], [])
            self.assertEqual(report["concurrent_renew"]["committed_renewal_rows"], 1)
            self.assertEqual(report["concurrent_renew"]["expires_ms_values"], ["1100"])
            self.assertTrue(report["verify"])


if __name__ == "__main__":
    unittest.main()
