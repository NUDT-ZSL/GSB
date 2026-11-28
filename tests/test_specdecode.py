from __future__ import annotations

import os
import tempfile
import unittest

from specdecode.compare import compare_policies, grid_policies
from specdecode.decoder import aggregate_request_stats, aggregate_results
from specdecode.generator import generate_canonical
from specdecode.loader import load_dataset
from specdecode.reference import compare_result_lists, reference_run
from specdecode.runner import SimulatedCrash, run_policy
from specdecode.schema import (
    canonical_fingerprint,
    parse_dataset,
    render_schema,
)
from specdecode.storage import (
    audit_stored_run,
    open_database,
    stored_request_results,
)


def _canonical():
    return generate_canonical(seed=777, request_count=12, tick_ms=2)


class SchemaAndGeneratorTests(unittest.TestCase):
    def test_generator_is_deterministic(self):
        self.assertEqual(_canonical(), _canonical())

    def test_all_schema_versions_are_semantically_identical(self):
        canonical = _canonical()
        expected = canonical_fingerprint(canonical)
        rendered = {}
        for version in (1, 2, 3):
            document = render_schema(canonical, version)
            rendered[version] = document
            self.assertEqual(
                canonical_fingerprint(parse_dataset(document)), expected
            )

        self.assertIsInstance(
            rendered[1]["policies"][0]["candidate_length"], int
        )
        self.assertEqual(
            rendered[1]["policies"][0]["acceptance_threshold_permille"] * 10,
            rendered[2]["speculationPolicies"][0]["acceptanceThresholdBps"],
        )
        self.assertEqual(
            rendered[2]["speculationPolicies"][0]["draftWidth"]["kind"],
            "fixed",
        )
        self.assertTrue(
            rendered[3]["speculativeStrategies"][0][
                "candidateLengthExpression"
            ].startswith("fixed:")
        )
        self.assertEqual(
            rendered[2]["requestStream"][0]["arrivalMilliseconds"],
            rendered[3]["requestStream"][0]["arrival"]["tick"] * 2,
        )
        self.assertEqual(
            rendered[3]["requestStream"][0]["deadline"]["millisecond"],
            canonical["requests"][0]["deadline_tick"] * 2,
        )


class LoaderTests(unittest.TestCase):
    def test_normalization_matches_generator_truth(self):
        dataset = load_dataset(render_schema(_canonical(), 1))
        self.assertEqual(len(dataset.requests), 12)
        self.assertTrue(any(not request.mapping_complete for request in dataset.requests))
        for request in dataset.requests:
            self.assertEqual(
                len(request.prompt_internal_ids),
                request.truth["normalized_length"],
            )
            self.assertEqual(
                {
                    name: list(positions)
                    for name, positions in request.special_token_positions.items()
                },
                request.truth["special_positions"],
            )
            self.assertEqual(
                list(request.unmappable_positions),
                request.truth["unmappable_positions"],
            )
        summary = dataset.load_summary(1)
        self.assertTrue(summary["all_truth_match"])
        self.assertGreaterEqual(
            len(summary["mapping_incomplete_requests"]), 1
        )


class DecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(render_schema(_canonical(), 1))
        cls.policy = cls.dataset.policies[0]
        cls.production_run = run_policy(cls.dataset, cls.policy)
        cls.results = {
            result["request_id"]: result
            for result in cls.production_run["requests"]
        }

    def test_replays_are_identical(self):
        second = run_policy(self.dataset, self.policy)
        self.assertEqual(self.production_run["requests"], second["requests"])
        self.assertEqual(
            self.production_run["aggregate"], second["aggregate"]
        )

    def test_template_positions_have_identical_distributions(self):
        template_results = [
            result
            for result in self.production_run["requests"]
            if result["request_class"] == "template"
        ]
        self.assertTrue(template_results)
        for result in template_results:
            self.assertEqual(result["fallbacks"], [])
            self.assertEqual(result["stats"]["acceptance_rate_bps"], 10000)
            for round_record in result["rounds"]:
                for decision in round_record["decisions"]:
                    self.assertTrue(decision["distributions_equal"])
                    self.assertEqual(decision["verdict"], "accepted")

    def test_first_rejection_truncates_and_resamples(self):
        found = False
        for result in self.production_run["requests"]:
            for round_record in result["rounds"]:
                rejected_position = round_record["rejected_position"]
                if rejected_position is None:
                    continue
                found = True
                decisions = round_record["decisions"]
                self.assertEqual(
                    decisions[rejected_position]["outcome"], "rejected_first"
                )
                self.assertFalse(decisions[rejected_position]["applied"])
                for index, decision in enumerate(decisions):
                    if index < rejected_position:
                        self.assertTrue(decision["applied"])
                        self.assertEqual(decision["verdict"], "accepted")
                    elif index > rejected_position:
                        self.assertFalse(decision["applied"])
                        self.assertEqual(
                            decision["outcome"],
                            "discarded_after_rejection",
                        )
                fallback = round_record["fallback"]
                self.assertIsNotNone(fallback)
                self.assertEqual(fallback["position"], rejected_position)
        self.assertTrue(found)

    def test_stats_equal_round_aggregation(self):
        for result in self.production_run["requests"]:
            recomputed = aggregate_request_stats(
                rounds=result["rounds"],
                fallbacks=result["fallbacks"],
                final_token_count=result["final_token_count"],
                target_forward_ticks_per_token=(
                    self.dataset.models.target.forward_ticks_per_token
                ),
                latency_budget_ticks=self.policy.latency_budget_ticks,
            )
            self.assertEqual(recomputed, result["stats"])
            reconstructed = []
            for round_record in result["rounds"]:
                for decision in round_record["decisions"]:
                    if decision["applied"]:
                        reconstructed.append(decision["draft_token_id"])
                if round_record["fallback"] is not None:
                    reconstructed.append(
                        round_record["fallback"]["resampled_internal_id"]
                    )
            self.assertEqual(reconstructed, result["final_internal_ids"])
        self.assertEqual(
            aggregate_results(self.production_run["requests"]),
            self.production_run["aggregate"],
        )

    def test_budget_breach_has_round_and_decisions(self):
        over_budget = [
            result
            for result in self.production_run["requests"]
            if not result["stats"]["within_budget"]
        ]
        self.assertTrue(over_budget)
        for result in over_budget:
            basis = result["stats"]["budget_breach_basis"]
            self.assertIsNotNone(basis)
            self.assertEqual(
                basis["round_index"], result["stats"]["first_exceeded_round"]
            )
            self.assertGreater(
                basis["cumulative_after_round"],
                result["stats"]["latency_budget_ticks"],
            )
            self.assertEqual(
                len(basis["round_decision_basis"]),
                len(result["rounds"][basis["round_index"]]["decisions"]),
            )


class ReferenceAndPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(render_schema(_canonical(), 2))
        cls.policy = cls.dataset.policies[1]

    def test_reference_decoder_matches_production(self):
        production = run_policy(self.dataset, self.policy)["requests"]
        reference = reference_run(self.dataset, self.policy)
        self.assertEqual(compare_result_lists(reference, production), [])

    def test_crash_resume_matches_uninterrupted_run(self):
        full = run_policy(self.dataset, self.policy)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "run.sqlite")
            with self.assertRaises(SimulatedCrash):
                run_policy(
                    self.dataset,
                    self.policy,
                    database_path=db_path,
                    crash_after_new_commits=3,
                )

            connection = open_database(db_path)
            try:
                committed = connection.execute(
                    "SELECT COUNT(*) FROM requests"
                ).fetchone()[0]
                self.assertEqual(committed, 3)
                self.assertEqual(
                    connection.execute(
                        "SELECT MAX(request_seq) FROM requests"
                    ).fetchone()[0],
                    2,
                )
                runs = connection.execute(
                    "SELECT completed_requests, status FROM runs"
                ).fetchone()
                self.assertEqual(runs["completed_requests"], 3)
                self.assertEqual(runs["status"], "running")
            finally:
                connection.close()

            resumed = run_policy(
                self.dataset, self.policy, database_path=db_path
            )
            self.assertEqual(resumed["requests"], full["requests"])
            self.assertEqual(resumed["aggregate"], full["aggregate"])
            self.assertEqual(
                resumed["persistence"]["resumed_from_committed_count"], 3
            )

            connection = open_database(db_path)
            try:
                persisted = stored_request_results(
                    connection, resumed["run_key"]
                )
                self.assertEqual(persisted, full["requests"])
                audit = audit_stored_run(
                    connection, resumed["run_key"], self.dataset
                )
                self.assertTrue(audit["ok"], audit["errors"])
            finally:
                connection.close()


class ComparisonTests(unittest.TestCase):
    def test_grid_comparison_reports_flips_and_reference_verification(self):
        dataset = load_dataset(render_schema(_canonical(), 3))
        policies = grid_policies(
            [4, 8], [5000, 9000], budget_ticks=140
        )
        report = compare_policies(
            dataset, policies, independently_verify=True
        )
        self.assertEqual(len(report["strategies"]), 4)
        self.assertTrue(
            all(item["reference_verified"] for item in report["strategies"])
        )
        self.assertGreaterEqual(
            report["comparisons_against_baseline"][1][
                "flipped_request_count"
            ],
            1,
        )
        self.assertIn(
            report["conclusion"]["recommended_policy_id"],
            [policy.policy_id for policy in policies],
        )
        self.assertEqual(
            set(report["conclusion"]["best_by_request_class"]),
            {"template", "reroute", "transfer"},
        )
        for strategy in report["strategies"]:
            self.assertEqual(
                set(strategy["request_class_metrics"]),
                {"template", "reroute", "transfer"},
            )


if __name__ == "__main__":
    unittest.main()
