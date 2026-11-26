from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from index_diag.analysis import (
    analyze_jsonl_files,
    deterministic_byte_view,
)
from index_diag.config import load_config
from index_diag.errors import (
    DiagnosticError,
    E_BAD_DOC,
    E_BATCH_ROLLBACK,
    E_DUP_DOC,
    E_POSTING_INVALID,
    E_RESOURCE_LIMIT,
)
from index_diag.postings import (
    Posting,
    append_documents,
    check_postings_structure,
    compute_term_statistics,
    postings_from_documents,
    validate_postings_against_documents,
)
from index_diag.resources import ResourceGuard, ResourceLimits

from .helpers import document, make_config, write_json, write_jsonl


class AnalysisPostingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        config_path = self.root / "config.json"
        write_json(config_path, make_config())
        self.config = load_config(config_path)
        self.batch = write_jsonl(
            self.root / "batch.jsonl",
            [
                document(1, 1, ["a", "b", "a"]),
                document(1, 2, ["b", "c", "b", "d"]),
                document(2, 3, ["a", "d"]),
            ],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_analysis_is_byte_deterministic(self) -> None:
        first = analyze_jsonl_files([self.batch], self.config)
        second = analyze_jsonl_files([self.batch], self.config)
        self.assertEqual(first.statistics()["digest"], second.statistics()["digest"])
        self.assertEqual(
            deterministic_byte_view(first.documents),
            deterministic_byte_view(second.documents),
        )
        self.assertEqual(first.documents[0].terms, ("a", "b", "a"))
        self.assertEqual(first.documents[0].position_map["a"], (0, 2))

    def test_bad_documents_identify_doc_id(self) -> None:
        bad_length = document(1, 10, ["a", "b"])
        bad_length["doc_length"] = 99
        path = write_jsonl(self.root / "bad-length.jsonl", [bad_length])
        with self.assertRaises(DiagnosticError) as caught:
            analyze_jsonl_files([path], self.config)
        self.assertEqual(caught.exception.code, E_BAD_DOC)
        self.assertEqual(caught.exception.details["doc_id"], 10)

        bad_term = document(1, 11, ["a", "outside"])
        path = write_jsonl(self.root / "bad-term.jsonl", [bad_term])
        with self.assertRaises(DiagnosticError) as caught:
            analyze_jsonl_files([path], self.config)
        self.assertEqual(caught.exception.code, E_BAD_DOC)
        self.assertEqual(caught.exception.details["doc_id"], 11)

    def test_duplicate_and_rollback_documents(self) -> None:
        duplicate_path = write_jsonl(
            self.root / "duplicate.jsonl",
            [document(1, 1, ["a"]), document(1, 1, ["b"])],
        )
        with self.assertRaises(DiagnosticError) as caught:
            analyze_jsonl_files([duplicate_path], self.config)
        self.assertEqual(caught.exception.code, E_DUP_DOC)

        rollback_path = write_jsonl(
            self.root / "rollback.jsonl", [document(1, 9, ["a"])]
        )
        with self.assertRaises(DiagnosticError) as caught:
            analyze_jsonl_files(
                [rollback_path], self.config, last_batch_no=5
            )
        self.assertEqual(caught.exception.code, E_BATCH_ROLLBACK)

    def test_sorted_postings_and_independent_statistics(self) -> None:
        analysis = analyze_jsonl_files([self.batch], self.config)
        postings, metrics = postings_from_documents(analysis.documents)
        self.assertEqual([posting.doc_id for posting in postings["a"]], [1, 3])
        self.assertEqual(postings["a"][0].positions, (0, 2))
        statistics = compute_term_statistics(postings)
        self.assertEqual(statistics["a"]["doc_frequency"], 2)
        self.assertEqual(statistics["a"]["position_offset_sum"], 2)
        report = validate_postings_against_documents(
            postings,
            analysis.documents,
            expected_statistics=statistics,
        )
        self.assertEqual(report["documents"], 3)
        self.assertGreater(metrics["comparisons"], 0)

    def test_incremental_append_only_touches_new_terms(self) -> None:
        first = analyze_jsonl_files([self.batch], self.config)
        postings, _ = postings_from_documents(
            [doc for doc in first.documents if doc.batch_no == 1]
        )
        second = document(2, 4, ["d", "d"])
        from index_diag.analysis import analyze_document

        new_doc = analyze_document(
            second, self.config.vocabulary_set, source="<test>"
        )
        metrics = append_documents(
            postings,
            [new_doc],
            guard=ResourceGuard(self.config.resource_limits),
        )
        self.assertEqual(metrics["terms_touched"], 1)
        self.assertEqual([posting.doc_id for posting in postings["d"]], [2, 4])
        self.assertEqual([posting.doc_id for posting in postings["a"]], [1])

    def test_invalid_posting_order_and_duplicates_are_detected(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            check_postings_structure(
                {"a": [Posting(2, 1, (0,)), Posting(1, 1, (0,))]}
            )
        self.assertEqual(caught.exception.code, E_POSTING_INVALID)

        with self.assertRaises(DiagnosticError) as caught:
            check_postings_structure(
                {"a": [Posting(1, 1, (0,)), Posting(1, 1, (1,))]}
            )
        self.assertEqual(caught.exception.code, E_POSTING_INVALID)

    def test_resource_limits(self) -> None:
        limited_path = self.root / "limited-config.json"
        write_json(
            limited_path,
            make_config(max_batch_docs=1),
        )
        limited = load_config(limited_path)
        with self.assertRaises(DiagnosticError) as caught:
            analyze_jsonl_files([self.batch], limited)
        self.assertEqual(caught.exception.code, E_RESOURCE_LIMIT)

        truncated_path = self.root / "truncated-config.json"
        write_json(
            truncated_path,
            make_config(max_batch_docs=1, degrade_policy="truncate_batch"),
        )
        truncated = load_config(truncated_path)
        result = analyze_jsonl_files([self.batch], truncated)
        self.assertEqual(result.analyzed_count, 2)
        self.assertEqual(result.resource_actions[0]["action"], "truncate_batch")

        comparisons = ResourceGuard(ResourceLimits(max_comparisons=1))
        from index_diag.analysis import analyze_document

        docs = [
            analyze_document(
                document(1, index, ["a"]),
                self.config.vocabulary_set,
                source="<test>",
            )
            for index in (2, 3, 4)
        ]
        with self.assertRaises(DiagnosticError) as caught:
            postings_from_documents(docs, guard=comparisons)
        self.assertEqual(caught.exception.code, E_RESOURCE_LIMIT)

        memory = ResourceGuard(ResourceLimits(max_postings_bytes=10))
        with self.assertRaises(DiagnosticError) as caught:
            postings_from_documents(docs, guard=memory)
        self.assertEqual(caught.exception.code, E_RESOURCE_LIMIT)


if __name__ == "__main__":
    unittest.main()
