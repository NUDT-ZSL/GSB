from __future__ import annotations

import unittest

from sparse_spmm.strategy import select_strategy
from tests.helpers import load_config, test_case


class StrategyTests(unittest.TestCase):
    def test_decision_is_scored_sorted_and_reasoned(self) -> None:
        case = test_case()
        decision = select_strategy(
            case.csr, case.B, case.sample_config, case.strategy_config
        )
        self.assertEqual(decision["strategy_id"], case.strategy["strategy_id"])
        self.assertTrue(decision["reason"])
        self.assertGreater(len(decision["candidates"]), 1)
        eligible = [candidate for candidate in decision["candidates"] if candidate["eligible"]]
        self.assertEqual(eligible[0]["id"], decision["strategy_id"])
        scores = [candidate["score"] for candidate in eligible]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertIn("reference_metrics", decision)
        self.assertIn("strategy_signature", decision)
        for candidate in decision["candidates"]:
            self.assertEqual(
                set(candidate["score_components"]),
                {"block_fit", "order_fit", "distribution_fit", "prior"},
            )

    def test_all_three_distribution_samples_select_valid_strategy(self) -> None:
        strategies = load_config("strategies.json")
        for name in ("sample_uniform.json", "sample_powerlaw.json", "sample_scsr.json"):
            with self.subTest(sample=name):
                case = test_case_for(name, strategies)
                decision = select_strategy(
                    case.csr, case.B, case.sample_config, strategies
                )
                self.assertGreaterEqual(decision["block_size"], 1)
                self.assertLessEqual(decision["block_size"], case.csr.cols)


def test_case_for(name: str, strategies: dict):
    from sparse_spmm.runner import prepare_case

    return prepare_case(load_config(name), strategies)


if __name__ == "__main__":
    unittest.main()

