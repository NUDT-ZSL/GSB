from __future__ import annotations

import copy
import dataclasses
import unittest

from fusion_diag.canonical import stable_float, stable_sum
from fusion_diag.config import ROUTE_ORDER, semantic_digest
from fusion_diag.engine import build_query_plan, load_workspace
from fusion_diag.errors import CandidateDrop
from fusion_diag.fusion import (
    align_candidates,
    finalize_fusion,
    validate_fusion_recomputation,
)
from fusion_diag.recall import run_recall

from .support import SampleWorkspace


class RecallFusionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace = SampleWorkspace()
        cls.loaded = load_workspace(
            docs_path=cls.workspace.path("docs.jsonl"),
            queries_path=cls.workspace.path("queries.jsonl"),
            vocab_path=cls.workspace.path("vocab.txt"),
            config_path=cls.workspace.path("config_v2"),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workspace.cleanup()

    def test_four_recall_routes_are_bounded_and_sorted(self) -> None:
        query = self.loaded.query_by_id["q0001"]
        routes = run_recall(self.loaded.corpus, query, self.loaded.config)
        self.assertEqual(set(routes), set(ROUTE_ORDER))
        for route in ROUTE_ORDER:
            candidates = routes[route]["candidates"]
            self.assertLessEqual(
                len(candidates), self.loaded.config.recalls[route]["limit"]
            )
            keys = [(-item["score"], item["doc_id"]) for item in candidates]
            self.assertEqual(keys, sorted(keys))
            self.assertEqual(
                len({item["doc_id"] for item in candidates}), len(candidates)
            )

    def test_contributions_recompute_fusion_score(self) -> None:
        query = self.loaded.query_by_id["q0006"]
        routes = run_recall(self.loaded.corpus, query, self.loaded.config)
        fused = finalize_fusion(
            query,
            align_candidates(query, routes, self.loaded.config),
            self.loaded.config,
        )
        self.assertTrue(fused["candidates"])
        for candidate in fused["candidates"]:
            self.assertTrue(validate_fusion_recomputation(candidate))
            expected = stable_sum(
                [source["contribution"] for source in candidate["sources"]]
            )
            self.assertEqual(expected, candidate["fusion_score"])
            self.assertIn("discarded_route_hits", candidate["dedup"])

    def test_duplicate_candidate_raises_candidate_drop_with_doc_id(self) -> None:
        query = self.loaded.query_by_id["q0001"]
        routes = run_recall(self.loaded.corpus, query, self.loaded.config)
        duplicate = copy.deepcopy(routes["lexical"]["candidates"][0])
        routes["lexical"]["candidates"].append(duplicate)
        with self.assertRaises(CandidateDrop) as caught:
            align_candidates(query, routes, self.loaded.config)
        self.assertEqual(caught.exception.code, "E_CANDIDATE_DROP")
        self.assertEqual(caught.exception.detail["doc_id"], duplicate["doc_id"])

    def test_tolerance_band_uses_doc_id_for_near_ties(self) -> None:
        payload = copy.deepcopy(self.loaded.config.payload)
        payload["fusion"]["topk_compare"] = "tolerance"
        payload["fusion"]["tolerance"] = 1e-8
        config = dataclasses.replace(
            self.loaded.config,
            payload=payload,
            digest=semantic_digest(payload),
        )
        query = self.loaded.query_by_id["q0001"]
        alignment = {
            "normalization": "minmax",
            "candidates": [
                {
                    "doc_id": "d0002",
                    "fusion_score": 0.50000000001,
                    "sources": [],
                    "dedup": {},
                },
                {
                    "doc_id": "d0001",
                    "fusion_score": 0.5,
                    "sources": [],
                    "dedup": {},
                },
                {
                    "doc_id": "d0003",
                    "fusion_score": 0.2,
                    "sources": [],
                    "dedup": {},
                },
            ],
            "normalized_route_candidate_counts": {},
            "trace": {
                "merged_source_count": 0,
                "normalization_calculations": 0,
                "fusion_weight_count": 4,
            },
        }
        query = dataclasses.replace(query, top_k=2)
        result = finalize_fusion(query, alignment, config)
        self.assertEqual([item["doc_id"] for item in result["top_k"]], ["d0001", "d0002"])

    def test_strict_comparison_keeps_higher_score_first(self) -> None:
        payload = copy.deepcopy(self.loaded.config.payload)
        payload["fusion"]["topk_compare"] = "strict"
        config = dataclasses.replace(
            self.loaded.config,
            payload=payload,
            digest=semantic_digest(payload),
        )
        query = dataclasses.replace(
            self.loaded.query_by_id["q0001"], top_k=2
        )
        alignment = {
            "normalization": "minmax",
            "candidates": [
                {
                    "doc_id": "d0002",
                    "fusion_score": 0.50000000001,
                    "sources": [],
                    "dedup": {},
                },
                {
                    "doc_id": "d0001",
                    "fusion_score": 0.5,
                    "sources": [],
                    "dedup": {},
                },
            ],
            "normalized_route_candidate_counts": {},
            "trace": {
                "merged_source_count": 0,
                "normalization_calculations": 0,
                "fusion_weight_count": 4,
            },
        }
        result = finalize_fusion(query, alignment, config)
        self.assertEqual(
            [item["doc_id"] for item in result["top_k"]],
            ["d0002", "d0001"],
        )

    def test_first_hit_dedup_keeps_first_route_representative(self) -> None:
        payload = copy.deepcopy(self.loaded.config.payload)
        payload["fusion"]["dedup_policy"] = "first_hit"
        config = dataclasses.replace(
            self.loaded.config,
            payload=payload,
            digest=semantic_digest(payload),
        )
        query = self.loaded.query_by_id["q0001"]
        routes = run_recall(self.loaded.corpus, query, config)
        aligned = align_candidates(query, routes, config)
        merged = [
            candidate
            for candidate in aligned["candidates"]
            if len(candidate["sources"]) > 1
        ]
        self.assertTrue(merged)
        for candidate in merged:
            self.assertEqual(
                candidate["dedup"]["retained_source"],
                candidate["sources"][0]["route"],
            )

    def test_subnormal_protection(self) -> None:
        self.assertEqual(stable_float(1e-310), 0.0)
        self.assertEqual(stable_float(-1e-310), 0.0)

    def test_route_dependency_scope_changes_only_for_relevant_query(self) -> None:
        revised = load_workspace(
            docs_path=self.workspace.path("variants/docs_tf_revision.jsonl"),
            queries_path=self.workspace.path("queries.jsonl"),
            vocab_path=self.workspace.path("vocab.txt"),
            config_path=self.workspace.path("config_v2"),
        )
        q1_base = build_query_plan(
            self.loaded.corpus,
            self.loaded.query_by_id["q0001"],
            self.loaded.config,
        )
        q1_revised = build_query_plan(
            revised.corpus,
            revised.query_by_id["q0001"],
            revised.config,
        )
        q2_base = build_query_plan(
            self.loaded.corpus,
            self.loaded.query_by_id["q0002"],
            self.loaded.config,
        )
        q2_revised = build_query_plan(
            revised.corpus,
            revised.query_by_id["q0002"],
            revised.config,
        )
        self.assertNotEqual(q1_base.route_keys, q1_revised.route_keys)
        self.assertEqual(q2_base.route_keys, q2_revised.route_keys)


if __name__ == "__main__":
    unittest.main()

