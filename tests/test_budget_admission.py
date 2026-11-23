import unittest

from sampling_bench.admission import (
    DEFAULT_PROFILES,
    arbitrate,
    evaluate_profile,
    priority_order,
)
from sampling_bench.budget import (
    BudgetLedger,
    BudgetLimits,
    request_cache_entries,
)
from sampling_bench.dataset import generate_dataset, load_requests
from sampling_bench.errors import (
    E_CACHE_BUDGET,
    E_SLOT_BUDGET,
    E_STEP_BUDGET,
)
from pathlib import Path


class BudgetAdmissionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(__file__).parent / "_data_budget"
        generate_dataset(cls.data_dir, num_requests=10, seed=909)
        cls.requests = load_requests(cls.data_dir)

    def test_priority_order_deterministic(self):
        ordered = priority_order(self.requests)
        keys = [
            (r.priority, r.target_length, request_cache_entries(r), r.request_id)
            for r in ordered
        ]
        self.assertEqual(keys, sorted(keys))

    def test_step_budget_rejection(self):
        limits = BudgetLimits(step_budget=5, cache_budget=10_000, slot_budget=10)
        evaluation = evaluate_profile(self.requests, DEFAULT_PROFILES[0], limits, 100)
        self.assertIn(E_STEP_BUDGET, {d.code for d in evaluation.rejected})

    def test_cache_budget_rejection(self):
        limits = BudgetLimits(step_budget=10_000, cache_budget=4, slot_budget=10)
        evaluation = evaluate_profile(self.requests, DEFAULT_PROFILES[0], limits, 100)
        self.assertIn(E_CACHE_BUDGET, {d.code for d in evaluation.rejected})

    def test_slot_budget_rejection(self):
        limits = BudgetLimits(step_budget=10_000, cache_budget=10_000, slot_budget=1)
        evaluation = evaluate_profile(self.requests, DEFAULT_PROFILES[0], limits, 1)
        self.assertIn(E_SLOT_BUDGET, {d.code for d in evaluation.rejected})

    def test_arbitration_is_deterministic(self):
        limits = BudgetLimits(22, 18, 2)
        first = arbitrate(self.requests, limits, max_waves=2).to_dict()
        second = arbitrate(self.requests, limits, max_waves=2).to_dict()
        self.assertEqual(first, second)

    def test_ledger_reserve_release_returns_to_zero(self):
        limits = BudgetLimits(100, 100, 10)
        ledger = BudgetLedger(limits)
        for request in self.requests[:3]:
            ledger.reserve(request)
        for request in self.requests[:3]:
            ledger.release(request)
        self.assertEqual(ledger.used(), {"steps": 0, "cache": 0, "slots": 0})


if __name__ == "__main__":
    unittest.main()
