import unittest

from intentkit.budget import BudgetLimits, admit, estimate_keyword_table_bytes
from intentkit.generator import build_config_v1, generate_tickets
from intentkit.matcher import build_keyword_entries
from intentkit.selector import choose_path, estimate_approx_comparisons, observe


class SelectorBudgetTests(unittest.TestCase):
    def setUp(self):
        self.config = build_config_v1()
        self.tickets = generate_tickets(count=96)

    def test_decision_is_deterministic(self):
        first = choose_path(self.tickets, self.config).to_dict()
        second = choose_path(self.tickets, self.config).to_dict()
        self.assertEqual(first, second)

    def test_observations_cover_all_intents(self):
        obs = observe(self.tickets, self.config)
        self.assertEqual(obs.enabled_intent_total, len(self.config.rules))
        self.assertEqual(obs.ticket_count, 96)
        self.assertGreater(obs.avg_query_codepoints, 0)

    def test_approx_estimate_lower_for_large_scale(self):
        big = generate_tickets(count=400)
        decision = choose_path(big, self.config)
        obs = observe(big, self.config)
        exact_est = decision.estimated_exact_comparisons
        approx_est = estimate_approx_comparisons(self.config, obs)
        self.assertTrue(decision.streaming)
        self.assertLess(approx_est, exact_est)
        self.assertEqual(decision.path, "approximate")

    def test_streaming_batches_keep_all_tickets(self):
        big = generate_tickets(count=300)
        decision = choose_path(big, self.config)
        admission = admit(
            big,
            self.config,
            decision,
            BudgetLimits(max_total_comparisons=10**12, max_resident_bytes=10**12),
        )
        admitted = sum(len(batch) for batch in admission.batches)
        self.assertEqual(admitted, 300)
        self.assertEqual(admission.downgrade_category, "batch_downgrade")

    def test_match_budget_rejects_excess_tickets(self):
        decision = choose_path(self.tickets, self.config)
        admission = admit(
            self.tickets,
            self.config,
            decision,
            BudgetLimits(max_total_comparisons=25_000, max_resident_bytes=10**12),
        )
        self.assertTrue(admission.admitted)
        self.assertGreater(admission.rejected_ticket_total, 0)
        self.assertEqual(
            admission.admitted_ticket_total + admission.rejected_ticket_total,
            len(self.tickets),
        )

    def test_resource_limit_triggers(self):
        entries = build_keyword_entries(self.config)
        resident = estimate_keyword_table_bytes(entries)
        decision = choose_path(self.tickets, self.config)
        admission = admit(
            self.tickets,
            self.config,
            decision,
            BudgetLimits(
                max_total_comparisons=10**12,
                max_resident_bytes=max(resident - 1, 1),
            ),
        )
        self.assertFalse(admission.admitted)
        self.assertEqual(admission.error_code, "E_RESOURCE_LIMIT")


if __name__ == "__main__":
    unittest.main()
