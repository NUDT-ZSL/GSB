from __future__ import annotations

import copy
import unittest

from sampling_compat.errors import BusinessError, ErrorCode
from sampling_compat.selfcheck import run_self_check, verify_consistency
from tests.helpers import temporary_app


class SelfCheckTests(unittest.TestCase):
    def test_complete_self_check_passes(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        report = run_self_check(app)
        self.assertTrue(report["ok"])
        self.assertEqual(report["decision_checks"], 3)
        self.assertEqual(report["reference_checks"], 3)
        self.assertEqual(report["migration_checks"], 3)
        self.assertEqual(report["adjudication_checks"], 3)
        self.assertLess(report["max_reference_weight_delta"], 1e-12)

    def test_tampered_decision_token_is_detected(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        run_self_check(app)
        app.decisions["req-0001"]["token_ids"][0] ^= 1
        with self.assertRaises(BusinessError) as raised:
            verify_consistency(app)
        self.assertEqual(raised.exception.code, ErrorCode.INCONSISTENT)
        self.assertEqual(raised.exception.request_id, "req-0001")
        self.assertEqual(raised.exception.details["category"], "decision_recompute")

    def test_migration_drop_field_trace_tamper_is_detected(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        run_self_check(app)
        record = app.migration_records["req-0002"]
        record["downgrade"]["dropped_fields"][0]["field"] = "non_existent_field"
        with self.assertRaises(BusinessError) as raised:
            verify_consistency(app)
        self.assertEqual(raised.exception.code, ErrorCode.INCONSISTENT)
        self.assertEqual(raised.exception.request_id, "req-0002")
        self.assertEqual(
            raised.exception.details["category"],
            "migration_traceability",
        )

    def test_snapshot_decision_tamper_is_detected(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        run_self_check(app)
        tampered = copy.deepcopy(app.decisions)
        tampered["req-0003"]["token_ids"] = [0]
        updates = {
            "sampling_decisions": tampered,
        }
        app.snapshot_repo.commit_many(updates)
        with self.assertRaises(BusinessError) as raised:
            verify_consistency(app)
        self.assertEqual(raised.exception.code, ErrorCode.INCONSISTENT)
        self.assertEqual(raised.exception.details["category"], "snapshot_decisions")


if __name__ == "__main__":
    unittest.main()
