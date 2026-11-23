from __future__ import annotations

import unittest
from pathlib import Path

from sparse_recall.canonical import canonical_json_line
from sparse_recall.errors import (
    E_DUPLICATE_ID,
    E_TERM_INVALID,
    E_TERM_UNKNOWN,
)
from sparse_recall.ingest import ingest_dataset
from sparse_recall.inverted import (
    NeumaierAccumulator,
    accumulate_batch,
    merge_global_partitions,
    recompute_stats_from_documents,
    verify_partition_rollup,
)
from sparse_recall.projection import project_document, sparse_dot
from sparse_recall.scoring import rank_scores
from sparse_recall.weights import generate_term_weight_table, save_weight_table

from tests._fixtures import load_inputs, make_dataset


class IngestStatisticsProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary, cls.data = make_dataset()
        cls.weights, cls.ingested = load_inputs(cls.data)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_dataset_is_deterministic(self) -> None:
        second_temporary, second = make_dataset()
        try:
            for filename in (
                "documents.jsonl",
                "queries.jsonl",
                "term_weights.json",
                "dataset_manifest.json",
            ):
                self.assertEqual(
                    (self.data / filename).read_bytes(),
                    (second / filename).read_bytes(),
                    filename,
                )
        finally:
            second_temporary.cleanup()

    def test_ingest_is_order_independent(self) -> None:
        reversed_docs = list(reversed((self.data / "documents.jsonl").read_text().splitlines()))
        reversed_queries = list(
            reversed((self.data / "queries.jsonl").read_text().splitlines())
        )
        reverse_dir = Path(self.temporary.name) / "reversed-data"
        reverse_dir.mkdir()
        (reverse_dir / "documents.jsonl").write_text(
            "\n".join(reversed_docs) + "\n",
            encoding="utf-8",
        )
        (reverse_dir / "queries.jsonl").write_text(
            "\n".join(reversed_queries) + "\n",
            encoding="utf-8",
        )
        reversed_ingest = ingest_dataset(
            reverse_dir / "documents.jsonl",
            reverse_dir / "queries.jsonl",
            self.weights,
        )
        self.assertEqual(
            [document.to_dict() for document in self.ingested.documents],
            [document.to_dict() for document in reversed_ingest.documents],
        )
        self.assertEqual(
            [query.to_dict() for query in self.ingested.queries],
            [query.to_dict() for query in reversed_ingest.queries],
        )

    def test_unknown_term_duplicate_and_doc_len_errors(self) -> None:
        temp = Path(self.temporary.name) / "invalid-data"
        temp.mkdir()
        weights = generate_term_weight_table(["t000", "t001"], dimension=8, nnz_per_term=2)
        save_weight_table(str(temp / "term_weights.json"), weights)
        docs = [
            {
                "batch_no": 1,
                "doc_id": "doc-a",
                "doc_len": 1,
                "terms": {"t000": 1},
            },
            {
                "batch_no": 2,
                "doc_id": "doc-b",
                "doc_len": 1,
                "terms": {"missing": 1},
            },
            {
                "batch_no": 3,
                "doc_id": "doc-c",
                "doc_len": 2,
                "terms": {"t001": 1},
            },
            {
                "batch_no": 4,
                "doc_id": "doc-a",
                "doc_len": 1,
                "terms": {"t001": 1},
            },
        ]
        queries = [
            {
                "batch_no": 100,
                "query_id": "q",
                "terms": {"t000": 1},
                "top_k": 1,
            }
        ]
        (temp / "documents.jsonl").write_bytes(
            b"".join(canonical_json_line(row) for row in docs)
        )
        (temp / "queries.jsonl").write_bytes(
            b"".join(canonical_json_line(row) for row in queries)
        )
        result = ingest_dataset(
            temp / "documents.jsonl",
            temp / "queries.jsonl",
            weights,
        )
        codes = {error["code"] for error in result.errors}
        self.assertIn(E_TERM_UNKNOWN, codes)
        self.assertIn(E_TERM_INVALID, codes)
        self.assertIn(E_DUPLICATE_ID, codes)

    def test_partition_recompute_and_global_rollup(self) -> None:
        partitions = []
        for batch_no, documents in sorted(self.ingested.doc_batches.items()):
            partition, postings = accumulate_batch(
                documents,
                batch_no=batch_no,
                chunk_size=5,
                compensated=True,
            )
            expected_partition, expected_postings = recompute_stats_from_documents(
                documents,
                compensated=True,
            )
            self.assertEqual(partition, expected_partition)
            self.assertEqual(postings, expected_postings)
            for term in partition["terms"]:
                self.assertEqual(
                    partition["terms"][term]["df"],
                    len(postings[term]),
                )
            partitions.append(partition)
        global_stats = merge_global_partitions(partitions, compensated=True)
        self.assertEqual(
            global_stats["document_count"],
            sum(partition["document_count"] for partition in partitions),
        )
        self.assertEqual(verify_partition_rollup(partitions, global_stats), [])

    def test_compensated_sum_protects_small_addend(self) -> None:
        values = [1.0e16, 1.0, -1.0e16]
        simple = 0.0
        compensated = NeumaierAccumulator()
        for value in values:
            simple += value
            compensated.add(value)
        self.assertEqual(simple, 0.0)
        self.assertEqual(compensated.value, 1.0)

    def test_projection_is_repeatable_and_dot_is_symmetric(self) -> None:
        first = project_document(self.ingested.documents[0], self.weights)
        second = project_document(self.ingested.documents[0], self.weights)
        self.assertEqual(first, second)
        self.assertEqual(first["dimension"], 64)
        other = project_document(self.ingested.documents[1], self.weights)
        left = sparse_dot(first["dims"], other["dims"])
        right = sparse_dot(other["dims"], first["dims"])
        self.assertEqual(left, right)

    def test_topk_tie_break_and_tolerance_stability(self) -> None:
        strict = rank_scores([("b", 1.0), ("a", 1.0)], top_k=2)
        self.assertEqual([row["doc_id"] for row in strict], ["a", "b"])

        score_sets = [
            [("doc-b", 1.0 + 1.0e-11), ("doc-a", 1.0), ("doc-c", 1.0 - 1.0e-11)],
            [("doc-b", 1.0 - 2.0e-11), ("doc-a", 1.0), ("doc-c", 1.0 + 2.0e-11)],
        ]
        orders = []
        for scores in score_sets:
            for decimals in (6, 9, 12, 15):
                rows = rank_scores(
                    scores,
                    top_k=2,
                    threshold=1.0,
                    mode="tolerance",
                    tolerance=1.0e-8,
                    decimals=decimals,
                )
                orders.append([row["doc_id"] for row in rows])
        self.assertTrue(all(order == orders[0] for order in orders))

        strict_threshold = rank_scores(
            [("a", 1.0)],
            top_k=1,
            threshold=1.0,
            mode="strict",
        )
        self.assertEqual(strict_threshold, [])
        tolerance_threshold = rank_scores(
            [("a", 1.0)],
            top_k=1,
            threshold=1.0,
            mode="tolerance",
            tolerance=1.0e-9,
        )
        self.assertEqual(len(tolerance_threshold), 1)

    def test_subnormal_score_is_normalized(self) -> None:
        row = rank_scores([("a", 5.0e-324)], top_k=1)[0]
        self.assertEqual(row["score"], 0.0)


if __name__ == "__main__":
    unittest.main()
