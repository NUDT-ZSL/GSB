from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from elasticctl.service import dispatch, status, verify

from tests.common import load_fixture, sample_payload


class BudgetSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="elastic-budget-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _budget(self, **overrides: object) -> dict[str, object]:
        budget = copy.deepcopy(sample_payload("resource_budget.json"))
        budget["resourceBudget"].update(overrides)
        return budget

    def test_core_budget_cutoff_classifies_outcomes(self) -> None:
        db_file = self.root / "core.db"
        load_fixture(db_file)
        dispatch(db_file, at=120000)
        payload = dispatch(db_file, at=150000)
        scheduler = payload["scheduler"]
        self.assertGreaterEqual(scheduler["counts"]["notStarted"], 1)
        self.assertGreaterEqual(scheduler["counts"]["abandoned"], 1)
        self.assertTrue(
            any(
                item["reason"] == "core_hours_budget_exhausted"
                for item in scheduler["notStarted"]
            )
        )
        self.assertTrue(
            all(
                item["reason"]
                == "dropped_after_higher_priority_budget_cutoff"
                for item in scheduler["activelyAbandoned"]
            )
        )
        snapshot = status(db_file)
        self.assertLessEqual(
            snapshot["budget"]["coreUsedUnits"],
            snapshot["budget"]["poolCoreUnits"],
        )
        self.assertTrue(verify(db_file)["ok"])

    def test_round_time_budget_cutoff(self) -> None:
        db_file = self.root / "round-time.db"
        load_fixture(
            db_file,
            budget=self._budget(
                poolCoreHours="0.01",
                memoryLimitMiB=65536,
                roundTimeBudgetMs=800,
            ),
        )
        payload = dispatch(db_file, at=120000)
        scheduler = payload["scheduler"]
        self.assertEqual(scheduler["counts"]["succeeded"], 1)
        self.assertTrue(
            any(
                item["reason"] == "round_time_budget_exhausted"
                for item in scheduler["notStarted"]
            )
        )
        self.assertGreaterEqual(scheduler["counts"]["abandoned"], 1)

    def test_memory_budget_cutoff(self) -> None:
        db_file = self.root / "memory.db"
        load_fixture(
            db_file,
            budget=self._budget(
                poolCoreHours="0.01",
                memoryLimitMiB=1500,
                roundTimeBudgetMs=10000,
            ),
        )
        payload = dispatch(db_file, at=120000)
        scheduler = payload["scheduler"]
        self.assertEqual(scheduler["counts"]["succeeded"], 1)
        self.assertTrue(
            any(
                item["reason"] == "memory_budget_exhausted"
                for item in scheduler["notStarted"]
            )
        )
        snapshot = status(db_file)
        self.assertLessEqual(
            snapshot["budget"]["memoryReservedMiB"],
            snapshot["budget"]["memoryLimitMiB"],
        )

    def test_failure_uses_exponential_deterministic_backoff(self) -> None:
        budget = self._budget(
            poolCoreHours="0.01",
            memoryLimitMiB=65536,
            roundTimeBudgetMs=5000,
        )
        outcomes = copy.deepcopy(sample_payload("action_outcomes.json"))
        outcomes["rules"] = [
            {
                "ruleId": "r-search-out",
                "logicalNowMs": 120000,
                "outcome": "failure",
                "reason": "simulated-first-failure",
            },
            {
                "ruleId": "r-search-out",
                "logicalNowMs": 150000,
                "outcome": "failure",
                "reason": "simulated-second-failure",
            },
        ]

        db_one = self.root / "backoff-one.db"
        load_fixture(db_one, budget=budget, outcomes=outcomes)
        dispatch(db_one, at=120000)
        first = self._search_rule_state(db_one)
        dispatch(db_one, at=150000, workload_ids=["wl-search"])
        second = self._search_rule_state(db_one)
        self.assertEqual(first["consecutiveFailures"], 1)
        self.assertEqual(second["consecutiveFailures"], 2)
        self.assertGreater(
            second["backoffUntilMs"] - 150000,
            first["backoffUntilMs"] - 120000,
        )

        db_two = self.root / "backoff-two.db"
        load_fixture(db_two, budget=budget, outcomes=outcomes)
        dispatch(db_two, at=120000)
        dispatch(db_two, at=150000, workload_ids=["wl-search"])
        self.assertEqual(
            self._search_rule_state(db_two),
            second,
        )

    def _search_rule_state(self, db_file: Path) -> dict[str, object]:
        snapshot = status(db_file)
        search = next(
            workload
            for workload in snapshot["workloads"]
            if workload["id"] == "wl-search"
        )
        return next(
            state
            for state in search["ruleStates"]
            if state["ruleId"] == "r-search-out"
        )


if __name__ == "__main__":
    unittest.main()

