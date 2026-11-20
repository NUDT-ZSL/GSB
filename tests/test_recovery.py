from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from grayroll.store import Store
from grayroll.verify import run_verification
from tests.helpers import PROJECT_ROOT, SAMPLES


class RecoveryTests(unittest.TestCase):
    def test_two_crash_resume_runs_are_byte_identical_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary) / "verify"
            report = run_verification(PROJECT_ROOT, SAMPLES, workdir)
            self.assertEqual(report["status"], "VERIFIED")
            self.assertTrue(report["two_run_byte_identical"])
            self.assertTrue(report["checks"]["forced_termination_recovered"])
            self.assertTrue(report["checks"]["parallel_resume_idempotent"])
            self.assertTrue(report["checks"]["clean_equals_crash_resume"])

            store = Store(workdir / "repeat-1" / "state.db")
            try:
                ledger = store.export_ledger()
            finally:
                store.close()
            rollback_targets = {
                rollback["batch_id"]: rollback["completed_targets"]
                for rollback in ledger["rollbacks"]
            }
            self.assertEqual(rollback_targets["1"], [0.5, 0.1, 0.0])
            self.assertEqual(rollback_targets["4"], [0.15, 0.0])
            operation_keys = [tier["operation_key"] for tier in ledger["tiers"]]
            self.assertEqual(len(operation_keys), len(set(operation_keys)))
            committed = [
                tier for tier in ledger["tiers"] if tier["status"] == "COMMITTED"
            ]
            self.assertEqual(len(committed), len(ledger["tiers"]))


if __name__ == "__main__":
    unittest.main()
