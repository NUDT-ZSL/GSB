import unittest

from intentkit.consistency import adjudicate
from intentkit.errors import BizError
from intentkit.generator import build_config_v1, generate_tickets
from intentkit.matcher import match_approximate, match_exact


class ConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.config = build_config_v1()
        self.tickets = generate_tickets(count=72)

    def test_clean_run_has_full_consistency(self):
        exact = [match_exact(ticket, self.config) for ticket in self.tickets]
        approx = [match_approximate(ticket, self.config) for ticket in self.tickets]
        report = adjudicate(exact, approx)
        self.assertEqual(report["intent_consistency_rate"], 1.0)
        self.assertEqual(report["divergence_total"], 0)
        self.assertEqual(report["confidence_gap_spectrum"]["max"], 0.0)

    def test_forced_pruning_miss_is_classified_and_raised(self):
        force_skip = ("logistics_query", "快递单号")
        exact = [
            match_exact(ticket, self.config)
            for ticket in self.tickets
        ]
        approx = [
            match_approximate(ticket, self.config, force_skip=force_skip)
            for ticket in self.tickets
        ]
        report = adjudicate(exact, approx, raise_on_divergence=False)
        self.assertGreater(report["divergence_total"], 0)
        kinds = {item["kind"] for item in report["divergences"]}
        self.assertIn("candidate_pruning_miss", kinds)
        with self.assertRaises(BizError) as context:
            adjudicate(exact, approx, raise_on_divergence=True)
        self.assertEqual(context.exception.code, "E_PATH_DIVERGENCE")


if __name__ == "__main__":
    unittest.main()
