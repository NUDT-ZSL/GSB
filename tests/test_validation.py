from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from lockfair.engine import LockFairEngine
from lockfair.errors import BusinessError, ErrorCode
from lockfair.models import parse_bundle

from tests.helpers import demo_raw


class ValidationTests(unittest.TestCase):
    def assert_code(self, raw: dict, code: ErrorCode) -> BusinessError:
        with self.assertRaises(BusinessError) as captured:
            parse_bundle(raw)
        self.assertEqual(captured.exception.code, code)
        return captured.exception

    def test_valid_demo(self) -> None:
        bundle = parse_bundle(demo_raw())
        self.assertEqual(len(bundle.resources), 3)
        self.assertEqual(len(bundle.holders), 4)
        self.assertEqual(len(bundle.applications), 11)
        self.assertEqual(bundle.config.fairness_labels, ("gold", "silver", "bronze"))

    def test_duplicate_resource(self) -> None:
        raw = demo_raw()
        raw["resources"].append(copy.deepcopy(raw["resources"][0]))
        exc = self.assert_code(raw, ErrorCode.DUPLICATE_RESOURCE_KEY)
        self.assertEqual(exc.lock_key, raw["resources"][0]["lock_key"])

    def test_duplicate_application_seq(self) -> None:
        raw = demo_raw()
        raw["applications"].append(copy.deepcopy(raw["applications"][0]))
        exc = self.assert_code(raw, ErrorCode.DUPLICATE_APPLICATION_SEQ)
        self.assertEqual(exc.application_seq, raw["applications"][0]["seq"])

    def test_unknown_holder(self) -> None:
        raw = demo_raw()
        raw["applications"][0]["holder"] = "ghost"
        exc = self.assert_code(raw, ErrorCode.HOLDER_NOT_FOUND)
        self.assertEqual(exc.details["holder"], "ghost")

    def test_unknown_lock(self) -> None:
        raw = demo_raw()
        raw["applications"][0]["lock_key"] = "missing-lock"
        self.assert_code(raw, ErrorCode.LOCK_NOT_FOUND)

    def test_invalid_lease_duration(self) -> None:
        raw = demo_raw()
        raw["resources"][0]["lease_duration_ms"] = 0
        self.assert_code(raw, ErrorCode.INVALID_LEASE_DURATION)

    def test_invalid_holder_cap(self) -> None:
        raw = demo_raw()
        raw["holders"][0]["max_concurrent_grants"] = 0
        self.assert_code(raw, ErrorCode.INVALID_CONCURRENCY_LIMIT)

    def test_invalid_budget(self) -> None:
        raw = demo_raw()
        raw["config"]["round_budget_ms"] = 0
        self.assert_code(raw, ErrorCode.INVALID_ROUND_BUDGET)

    def test_invalid_fairness_label(self) -> None:
        raw = demo_raw()
        raw["applications"][0]["fairness_label"] = "platinum"
        exc = self.assert_code(raw, ErrorCode.INVALID_FAIRNESS_LABEL)
        self.assertEqual(exc.constraint, "fairness_label")

    def test_invalid_starvation_threshold(self) -> None:
        raw = demo_raw()
        raw["config"]["starvation_rounds"] = 0
        self.assert_code(raw, ErrorCode.INVALID_STARVATION_THRESHOLD)

    def test_impossible_fair_quota(self) -> None:
        raw = demo_raw()
        raw["config"]["fair_quota_min_grants"] = 10
        exc = self.assert_code(raw, ErrorCode.FAIR_QUOTA_UNSATISFIABLE)
        self.assertEqual(exc.constraint, "fair_quota")
        self.assertGreater(
            exc.details["fair_quota_min_grants"] * exc.details["min_grant_cost_ms"],
            exc.details["round_budget_ms"],
        )

    def test_invalid_input_writes_nothing(self) -> None:
        raw = demo_raw()
        raw["resources"].append(copy.deepcopy(raw["resources"][0]))
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nested" / "state.sqlite"
            engine = LockFairEngine(db_path)
            with self.assertRaises(BusinessError) as captured:
                engine.initialize(raw, force=True)
            self.assertEqual(captured.exception.code, ErrorCode.DUPLICATE_RESOURCE_KEY)
            self.assertFalse(db_path.exists())
            self.assertFalse((Path(directory) / "nested").exists())


if __name__ == "__main__":
    unittest.main()
