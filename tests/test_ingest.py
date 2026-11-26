from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fusion_diag.canonical import canonical_bytes
from fusion_diag.errors import FormatCorrupt
from fusion_diag.ingest import (
    load_documents,
    load_queries,
    load_vocabulary,
    prepare_corpus,
)

from .support import SampleWorkspace


class IngestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace = SampleWorkspace()
        cls.vocab = load_vocabulary(cls.workspace.path("vocab.txt"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workspace.cleanup()

    def test_prepared_corpus_is_byte_deterministic_and_presorted(self) -> None:
        docs = load_documents(self.workspace.path("docs.jsonl"), self.vocab)
        first = prepare_corpus(docs, self.vocab)
        second = prepare_corpus(tuple(reversed(docs)), self.vocab)
        self.assertEqual(
            canonical_bytes(first.prepared_payload()),
            canonical_bytes(second.prepared_payload()),
        )
        ordered = [
            (doc.category, -doc.quality_score, doc.doc_id)
            for doc in first.documents
        ]
        self.assertEqual(ordered, sorted(ordered))
        self.assertEqual(first.stats["document_count"], 32)

    def test_documents_and_queries_are_sorted_by_id(self) -> None:
        docs = load_documents(self.workspace.path("docs.jsonl"), self.vocab)
        queries = load_queries(
            self.workspace.path("queries.jsonl"),
            categories={doc.category for doc in docs},
        )
        self.assertEqual([doc.doc_id for doc in docs], sorted(doc.doc_id for doc in docs))
        self.assertEqual(
            [query.query_id for query in queries],
            sorted(query.query_id for query in queries),
        )

    def test_invalid_quality_reports_line_and_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text(
                '{"doc_id":"d1","terms":[{"term":"alpha","tf":1}],'
                '"doc_len":1,"category":"books","quality_score":1.2}\n',
                encoding="utf-8",
            )
            with self.assertRaises(FormatCorrupt) as caught:
                load_documents(path, self.vocab)
        self.assertEqual(caught.exception.line, 1)
        self.assertEqual(caught.exception.field, "quality_score")
        self.assertEqual(caught.exception.code, "E_FORMAT_CORRUPT")

    def test_unknown_term_reports_term_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text(
                '{"doc_id":"d1","terms":[{"term":"outside","tf":1}],'
                '"doc_len":1,"category":"books","quality_score":0.5}\n',
                encoding="utf-8",
            )
            with self.assertRaises(FormatCorrupt) as caught:
                load_documents(path, self.vocab)
        self.assertEqual(caught.exception.field, "terms[0].term")

    def test_empty_query_terms_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad_queries.jsonl"
            path.write_text(
                '{"query_id":"q1","terms":[],"top_k":5}\n',
                encoding="utf-8",
            )
            with self.assertRaises(FormatCorrupt) as caught:
                load_queries(path, categories={"books"})
        self.assertEqual(caught.exception.field, "terms")


if __name__ == "__main__":
    unittest.main()

