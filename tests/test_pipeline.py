from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from term_pipeline.adaptive import choose_path
from term_pipeline.budget import plan_admission
from term_pipeline.comparison import compare_paths
from term_pipeline.disambiguation import _score_senses
from term_pipeline.errors import E_PATH_DIVERGENCE, PipelineError
from term_pipeline.scoring import recompute_score
from tests.helpers import new_workspace, run_workspace, runtime


class PipelineTests(unittest.TestCase):
    def test_generation_is_byte_deterministic(self) -> None:
        left_temp, left = new_workspace()
        right_temp, right = new_workspace()
        self.addCleanup(left_temp.cleanup)
        self.addCleanup(right_temp.cleanup)
        for relative in (
            "config.json",
            "data/glossary.json",
            "data/context_features.json",
            "data/rules/rules-v1.json",
            "data/rules/rules-v2.json",
            "data/shards/shard-000.jsonl",
            "data/shards/shard-001.jsonl",
            "data/shards/shard-002.jsonl",
            "data/shards/shard-003.jsonl",
        ):
            self.assertEqual(
                (left / relative).read_bytes(),
                (right / relative).read_bytes(),
                relative,
            )

    def test_extraction_longest_first_tie_and_offsets(self) -> None:
        temp, root = new_workspace()
        self.addCleanup(temp.cleanup)
        run_workspace(root)
        state = runtime(root)
        docs = {doc["doc_id"]: doc for doc in state["documents"]}
        extracted = {
            item["doc_id"]: item["candidates"] for item in state["extracted"]
        }
        network_doc = next(
            doc for doc in state["documents"] if "网络协议" in doc["text"]
        )
        candidates = extracted[network_doc["doc_id"]]
        network = next(c for c in candidates if c["term"] == "网络协议")
        overlaps = [
            candidate
            for candidate in candidates
            if candidate["start"] == network["start"] and candidate is not network
        ]
        self.assertEqual(overlaps, [])
        for doc_id, rows in extracted.items():
            text = docs[doc_id]["text"]
            for candidate in rows:
                self.assertEqual(candidate["term"], text[candidate["start"] : candidate["end"]])

        micro = next(
            candidate
            for rows in extracted.values()
            for candidate in rows
            if candidate["term"] == "微内核"
        )
        self.assertEqual(micro["entry_no"], 20)
        self.assertEqual(micro["term_id"], "T_MICRO_20")

    def test_scores_are_independently_recomputable(self) -> None:
        temp, root = new_workspace()
        self.addCleanup(temp.cleanup)
        run_workspace(root)
        state = runtime(root)
        for doc in state["scored_documents"]:
            for candidate in doc["candidates"]:
                self.assertEqual(
                    recompute_score(candidate["score_breakdown"]),
                    candidate["confidence"],
                )
                self.assertEqual(
                    candidate["score_breakdown"]["components"][2]["name"],
                    "context_coverage",
                )

    def test_sense_tie_uses_lowest_sense_id(self) -> None:
        temp, root = new_workspace()
        self.addCleanup(temp.cleanup)
        run_workspace(root)
        state = runtime(root)
        entry = next(
            entry for entry in state["glossary"]["entries"] if entry["term_id"] == "T_PROTOCOL"
        )
        candidate = next(
            candidate
            for doc in state["scored_documents"]
            for candidate in doc["candidates"]
            if candidate["term_id"] == "T_PROTOCOL"
        )
        sense_id, score, detail, observed = _score_senses(
            candidate,
            entry,
            [],
            state["rules"],
            state["features"],
            state["config"],
        )
        self.assertEqual((sense_id, score, observed), (1, 0.0, []))
        self.assertEqual(
            [item["sense_id"] for item in detail["sense_scores"]], [1, 2]
        )

    def test_adaptive_selector_is_deterministic_and_covers_docs(self) -> None:
        temp, root = new_workspace()
        self.addCleanup(temp.cleanup)
        run_workspace(root)
        state = runtime(root)
        first = choose_path(
            state["documents"],
            state["scored_documents"],
            {
                item["doc_id"]: len(item["candidates"])
                for item in state["scored_documents"]
            },
            state["features"],
            state["config"],
        )
        second = choose_path(
            state["documents"],
            state["scored_documents"],
            {
                item["doc_id"]: len(item["candidates"])
                for item in state["scored_documents"]
            },
            state["features"],
            state["config"],
        )
        self.assertEqual(first, second)
        self.assertEqual(first["selected_path"], "exact")
        self.assertTrue(first["coverage"]["all_documents_covered"])
        self.assertEqual(
            first["coverage"]["covered_doc_ids"],
            sorted(first["coverage"]["covered_doc_ids"]),
        )
        self.assertGreater(first["comparison_estimates"]["exact"], 0)

        config = copy.deepcopy(state["config"])
        config["adaptive"]["exact_max_shards"] = 1
        constrained = choose_path(
            state["documents"],
            state["scored_documents"],
            {
                item["doc_id"]: len(item["candidates"])
                for item in state["scored_documents"]
            },
            state["features"],
            config,
        )
        self.assertEqual(constrained["selected_path"], "approximate")
        self.assertIn(
            "SHARD_COUNT_EXCEEDS_EXACT_LIMIT",
            constrained["reason_codes"],
        )

    def test_budget_degrades_with_explicit_trigger(self) -> None:
        temp, root = new_workspace()
        self.addCleanup(temp.cleanup)
        run_workspace(root)
        state = runtime(root)
        unlimited = plan_admission(
            state["documents"],
            state["scored_documents"],
            state["features"],
            state["config"],
            policy="report",
        )
        costs = [
            decision["observations"]["estimated_compare_cost"]
            for decision in unlimited["decisions"]
        ]
        budget = sum(costs[:3])
        config = copy.deepcopy(state["config"])
        config["budget"]["total_compare_budget"] = budget
        config["budget"]["max_resident_cache_mb"] = 1024
        degraded = plan_admission(
            state["documents"],
            state["scored_documents"],
            state["features"],
            config,
            policy="report",
        )
        self.assertTrue(degraded["degraded"])
        self.assertEqual(degraded["degradation"]["category"], "COMPARE_BUDGET")
        self.assertEqual(
            degraded["degradation"]["trigger_budget"],
            "total_compare_budget",
        )
        self.assertEqual(degraded["batch_sizes"], [3])
        self.assertTrue(degraded["decisions"][3]["observations"])

        priority_config = copy.deepcopy(config)
        priority_config["budget"]["total_compare_budget"] = 0
        priority_config["budget"]["max_resident_cache_mb"] = 0
        priority = plan_admission(
            state["documents"],
            state["scored_documents"],
            state["features"],
            priority_config,
            policy="report",
        )
        self.assertEqual(
            priority["degradation"]["category"], "COMPARE_BUDGET"
        )

        resource_config = copy.deepcopy(state["config"])
        resource_config["budget"]["total_compare_budget"] = 10**9
        resource_config["budget"]["max_resident_cache_mb"] = 0
        resource = plan_admission(
            state["documents"],
            state["scored_documents"],
            state["features"],
            resource_config,
            policy="report",
        )
        self.assertEqual(
            resource["degradation"]["category"], "RESOURCE_LIMIT"
        )

    def test_cross_path_reports_pruning_and_context_differences(self) -> None:
        temp, root = new_workspace()
        self.addCleanup(temp.cleanup)
        run_workspace(root)
        state = runtime(root)
        baseline = compare_paths(
            state["documents"],
            state["scored_documents"],
            state["glossary"],
            state["rules"],
            state["features"],
            state["path_decision"],
            state["config"],
            strict=False,
        )
        self.assertTrue(baseline["index_stats_comparable"])
        self.assertTrue(
            baseline["recomputation"]["all_scores_recomputed"]
        )

        pruning_decision = copy.deepcopy(state["path_decision"])
        pruning_decision["selected_path"] = "approximate"
        pruning_decision["approximate_limits"]["max_candidates_per_doc"] = 1
        pruning_decision["approximate_limits"]["max_features_per_candidate"] = 99
        pruning = compare_paths(
            state["documents"],
            state["scored_documents"],
            state["glossary"],
            state["rules"],
            state["features"],
            pruning_decision,
            state["config"],
            strict=False,
        )
        self.assertIn("candidate_pruning", pruning["difference_classes"])
        self.assertTrue(pruning["candidate_pruning_omissions"])
        with self.assertRaises(PipelineError) as caught:
            compare_paths(
                state["documents"],
                state["scored_documents"],
                state["glossary"],
                state["rules"],
                state["features"],
                pruning_decision,
                state["config"],
                strict=True,
            )
        self.assertEqual(caught.exception.code, E_PATH_DIVERGENCE)

        context_decision = copy.deepcopy(state["path_decision"])
        context_decision["selected_path"] = "approximate"
        context_decision["approximate_limits"]["max_candidates_per_doc"] = 99
        context_decision["approximate_limits"]["max_features_per_candidate"] = 1
        context = compare_paths(
            state["documents"],
            state["scored_documents"],
            state["glossary"],
            state["rules"],
            state["features"],
            context_decision,
            state["config"],
            strict=False,
        )
        self.assertFalse(context["candidate_pruning_omissions"])
        self.assertTrue(context["context_comparison_changes"])
        self.assertIn("context_comparison", context["difference_classes"])
        self.assertTrue(
            context["recomputation"]["all_scores_recomputed"]
        )

    def test_rule_version_targets_only_affected_documents(self) -> None:
        temp, root = new_workspace()
        self.addCleanup(temp.cleanup)
        run_workspace(root)
        config_path = root / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["rule_version"] = "rules-v2"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        run_workspace(root)
        diagnostics = json.loads(
            (root / "out" / "diagnostics.json").read_text(encoding="utf-8")
        )
        invalidation = diagnostics["rule_invalidation"]
        self.assertEqual(invalidation["scope"], "targeted_documents")
        self.assertGreater(invalidation["invalidated_doc_count"], 0)
        self.assertGreater(invalidation["reused_doc_count"], 0)
        self.assertEqual(
            sorted(
                invalidation["invalidated_doc_ids"]
                + invalidation["reused_doc_ids"]
            ),
            sorted(doc["doc_id"] for doc in runtime(root)["documents"]),
        )
        self.assertEqual(
            [stage["version"] for stage in diagnostics["stages"]],
            [1, 2, 3, 4, 5],
        )

