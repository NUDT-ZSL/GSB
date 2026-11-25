from __future__ import annotations

import copy
import math
import tempfile
import unittest
from pathlib import Path

from ragdiag.inputs import load_requests, load_schedule
from ragdiag.merge import merge_request, verify_answer_from_details
from ragdiag.retrieval import (
    build_index,
    load_corpus,
    retrieve,
)
from ragdiag.sample_generator import generate


class RetrievalMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        generate(root / "data")
        cls.config = load_schedule(root / "data" / "schedule.json")
        cls.documents = load_corpus(root / "data" / "evidence_corpus.jsonl")
        cls.index = build_index(cls.documents, cls.config)
        cls.requests = load_requests(root / "data" / "requests.jsonl")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_candidates_have_route_contributions_and_are_stable(self) -> None:
        first = retrieve(
            "Shanghai weather temperature humidity forecast",
            self.config,
            self.index,
        )
        second = retrieve(
            "Shanghai weather temperature humidity forecast",
            self.config,
            self.index,
        )
        self.assertEqual(first, second)
        self.assertTrue(first["candidates"])
        for candidate in first["candidates"]:
            self.assertTrue(candidate["contributions"])
            total = math.fsum(
                item["weighted"] for item in candidate["contributions"]
            )
            self.assertAlmostEqual(candidate["score"], total, places=8)
            for contribution in candidate["contributions"]:
                self.assertIn(
                    contribution["route"], {"inverted", "prefix", "vector"}
                )
        ordered = [
            (-item["score"], item["doc_id"]) for item in first["candidates"]
        ]
        self.assertEqual(ordered, sorted(ordered))

    def test_prefix_and_vector_routes_recall(self) -> None:
        result = retrieve("warehous invent", self.config, self.index)
        self.assertGreater(result["routes"]["prefix"]["matched_documents"], 0)
        self.assertGreater(result["routes"]["vector"]["matched_documents"], 0)
        self.assertTrue(
            any(
                item["route"] == "prefix"
                for candidate in result["candidates"]
                for item in candidate["contributions"]
            )
        )

    def test_equal_scores_break_ties_by_doc_id(self) -> None:
        documents = [
            {"doc_id": "doc-b", "text": "same evidence token", "tags": []},
            {"doc_id": "doc-a", "text": "same evidence token", "tags": []},
        ]
        index = build_index(documents, self.config)
        result = retrieve("same evidence token", self.config, index)
        self.assertGreaterEqual(len(result["candidates"]), 2)
        first = result["candidates"][0]
        second = result["candidates"][1]
        if first["score"] == second["score"]:
            self.assertLess(first["doc_id"], second["doc_id"])

    def test_merge_fragment_details_recompute_answer(self) -> None:
        request = next(
            item for item in self.requests if item["unit_id"] == "conv-001@1"
        )
        parsed = []
        from ragdiag.formats import load_repository, parse_tool_result

        root = Path(self.temp.name) / "data"
        repository = load_repository(root / "tool_results.json")
        for index, call in enumerate(request["tool_calls"]):
            parsed.append(
                parse_tool_result(
                    repository.entries[call["result_ref"]],
                    repository.snapshot_version,
                    call["result_format_version"],
                )
            )
        retrieval = retrieve(
            request["query_text"], self.config, self.index
        )
        corpus = {
            document["doc_id"]: document for document in self.documents
        }
        read_keys = [f"read-key-{index}" for index in range(len(parsed))]
        artifact = merge_request(
            request,
            parsed,
            retrieval,
            corpus,
            self.config,
            "merge-key",
            "retrieval-key",
            read_keys,
        )
        self.assertEqual(len(artifact["fragments"]), 3)
        self.assertEqual(
            [fragment["merge_position"] for fragment in artifact["fragments"]],
            [0, 1, 2],
        )
        self.assertEqual(
            verify_answer_from_details(request, artifact), []
        )
        mutated = copy.deepcopy(artifact)
        mutated["fragments"][1]["text"] += " tampered"
        self.assertTrue(
            verify_answer_from_details(request, mutated)
        )


if __name__ == "__main__":
    unittest.main()

