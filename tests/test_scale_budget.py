from __future__ import annotations

import copy
import unittest

from query_understanding.budget import BudgetPolicy, admit_budget
from query_understanding.errors import E_MATCH_BUDGET, E_RESOURCE_LIMIT
from query_understanding.scale import (
    PATH_APPROX,
    PATH_EXACT,
    ScalePolicy,
    choose_path,
    observe_scale,
)
from query_understanding.tokenize import tokenize_text
from query_understanding.util import canonical_json, read_json

from tests.common import DATA, load_engine, load_large, load_small


class ScaleBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = read_json(DATA / "config.json")
        cls.engine = load_engine()
        cls.small = load_small()
        cls.large = load_large()
        cls.scale_policy = ScalePolicy.from_dict(cls.config["scale_policy"])
        cls.budget_policy = BudgetPolicy.from_dict(cls.config["budget_policy"])

    def _observation(self, queries):
        tokens = 0
        for query in queries:
            normalized = self.engine.mappings.normalize(query["query_text"])
            tokens += len(tokenize_text(normalized, self.engine.dictionary))
        return observe_scale(
            query_count=len(queries),
            dictionary_entry_count=len(self.engine.dictionary.entries),
            total_tokens=tokens,
            memory_limit_mb=512.0,
            resident_memory_mb=48.0,
            memory_reserve_mb=32.0,
            resident_memory_source="unit_test_fixed",
        )

    def test_scale_jump_switches_path(self):
        small = choose_path(
            self._observation(self.small),
            self.scale_policy,
            self.budget_policy,
        )
        large = choose_path(
            self._observation(self.large),
            self.scale_policy,
            self.budget_policy,
        )
        self.assertEqual(small["selected_path"], PATH_EXACT)
        self.assertEqual(large["selected_path"], PATH_APPROX)
        self.assertIn(
            "QUERY_COUNT_OVER_EXACT_LIMIT",
            large["exact_threshold_failures"],
        )
        self.assertIn("cost_estimates", large)
        self.assertIn("observation", large)

    def test_same_observation_produces_identical_decision_bytes(self):
        observation = self._observation(self.large)
        first = choose_path(observation, self.scale_policy, self.budget_policy)
        second = choose_path(
            copy.deepcopy(observation),
            self.scale_policy,
            self.budget_policy,
        )
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(first["decision_checksum"], second["decision_checksum"])

    def test_selected_path_covers_all_queries(self):
        decision = choose_path(
            self._observation(self.large),
            self.scale_policy,
            self.budget_policy,
        )
        results = self.engine.process_many(
            self.large, path=decision["selected_path"]
        )
        self.assertEqual(len(results), len(self.large))
        self.assertEqual(
            [result["query_id"] for result in results],
            [query["query_id"] for query in self.large],
        )

    def test_budget_accept_downbatch_and_reject(self):
        observation = self._observation(self.large)
        normal = admit_budget(observation, PATH_APPROX, self.budget_policy)
        self.assertEqual(normal["status"], "accepted")

        tight = BudgetPolicy(
            total_compare_budget=2_000,
            memory_limit_mb=512.0,
            approximate_candidate_limit=48,
            minimum_batch_size=8,
            priority_order=("precision", "memory", "progress"),
        )
        downbatch = admit_budget(
            observation, PATH_APPROX, tight, allow_downbatch=True
        )
        self.assertEqual(downbatch["status"], "downbatch")
        self.assertLess(downbatch["batch_size"], len(self.large))
        self.assertGreaterEqual(downbatch["batch_size"], 8)

        rejected = admit_budget(
            observation, PATH_APPROX, tight, allow_downbatch=False
        )
        self.assertEqual(rejected["error_code"], E_MATCH_BUDGET)

        memory_observation = copy.deepcopy(observation)
        memory_observation["memory_available_mb"] = 0.01
        memory = admit_budget(
            memory_observation, PATH_APPROX, self.budget_policy
        )
        self.assertEqual(memory["error_code"], E_RESOURCE_LIMIT)
        self.assertIn("precision", normal["priority_order"])


if __name__ == "__main__":
    unittest.main()

