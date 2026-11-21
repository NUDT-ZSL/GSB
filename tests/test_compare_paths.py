from __future__ import annotations

import unittest

from query_understanding.compare import compare_paths
from query_understanding.errors import E_PATH_INCOMPARABLE

from tests.common import DATA, load_engine, load_small
from query_understanding.util import read_json


class ComparePathsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = read_json(DATA / "config.json")
        cls.engine = load_engine()
        cls.queries = load_small()

    def test_default_paths_are_comparable(self):
        report = compare_paths(self.engine, self.queries)
        self.assertTrue(report["comparable"])
        self.assertEqual(report["intent_agreement_rate"], 1.0)
        self.assertEqual(report["slot_offset_difference_count"], 0)
        self.assertEqual(report["slot_value_difference_count"], 0)
        self.assertEqual(
            report["confidence_bias_spectrum"]["statistics"]["max_abs"], 0.0
        )
        self.assertEqual(
            report["metrics"]["delta_approx_minus_exact"]["ndcg_at_k"], 0.0
        )
        self.assertEqual(report["error_code"], None)

    def test_aggressive_pruning_returns_path_incomparable(self):
        stress = load_engine(
            {
                "approximate": {
                    "max_candidates_per_token": 1,
                    "max_candidate_intents": 1,
                    "fuzzy_threshold": 0.62,
                }
            }
        )
        report = compare_paths(stress, self.queries)
        self.assertFalse(report["comparable"])
        self.assertEqual(report["error_code"], E_PATH_INCOMPARABLE)
        self.assertIsNotNone(report["first_incomparable_query"])
        self.assertGreater(
            report["difference_causes"]["candidate_pruning_miss"], 0
        )
        self.assertIn("comparable", report["message"])

    def test_disambiguation_override_is_attributed_separately(self):
        policy_stress = load_engine(
            {
                "approximate_disambiguation": {
                    "click_weight": 0.0,
                }
            }
        )
        report = compare_paths(policy_stress, self.queries)
        self.assertFalse(report["comparable"])
        self.assertEqual(report["error_code"], E_PATH_INCOMPARABLE)
        self.assertEqual(
            report["difference_causes"]["candidate_pruning_miss"], 0
        )
        self.assertGreater(
            report["difference_causes"]["disambiguation_policy"], 0
        )


if __name__ == "__main__":
    unittest.main()

