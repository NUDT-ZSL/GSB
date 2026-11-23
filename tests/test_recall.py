import unittest

from recall_fusion.config import FusionConfig
from recall_fusion.models import Candidate
from recall_fusion.recall import RecallIndex
from recall_fusion.sample_data import generate_documents, generate_queries


class RecallTest(unittest.TestCase):
    def setUp(self):
        self.docs = generate_documents(60, seed=777)
        self.queries = generate_queries(8, seed=777)
        self.index = RecallIndex(self.docs)
        self.config = FusionConfig.default()

    def test_all_four_channels_return_ordered_candidates(self):
        query = self.queries[0]
        caps = {name: 15 for name in
                ("inverted", "prefix", "category", "vector")}
        result = self.index.run_all(query, caps)
        self.assertEqual(set(result), {"inverted", "prefix", "category", "vector"})
        for channel, candidates in result.items():
            if channel == "category" and query.category_filter is None:
                self.assertEqual(candidates, [])
                continue
            self.assertLessEqual(len(candidates), 15)
            scores = [c.raw_score for c in candidates]
            self.assertEqual(scores, sorted(scores, reverse=True))
            for candidate in candidates:
                self.assertIsInstance(candidate, Candidate)
                self.assertEqual(candidate.channel, channel)

    def test_tie_break_is_doc_id_ascending(self):
        # Inject deterministic tie via category recall (equal quality scores).
        from recall_fusion.models import Document

        docs = [
            Document("d2", {"a": 1}, 1, "tech", 0.5, (1.0, 0.0)),
            Document("d1", {"a": 1}, 1, "tech", 0.5, (0.0, 1.0)),
            Document("d3", {"a": 1}, 1, "tech", 0.5, (0.0, 0.0)),
        ]
        from recall_fusion.models import Query

        index = RecallIndex(docs)
        query = Query("q1", ("a",), 10, category_filter="tech")
        hits = index.category_recall(query, 10)
        self.assertEqual([h.doc_id for h in hits], ["d1", "d2", "d3"])

    def test_recall_is_byte_reproducible(self):
        caps = {name: 12 for name in
                ("inverted", "prefix", "category", "vector")}
        first = self.index.run_all(self.queries[1], caps)
        second = RecallIndex(generate_documents(60, seed=777)).run_all(
            generate_queries(8, seed=777)[1], caps
        )
        self.assertEqual(
            [(c.doc_id, c.channel, c.raw_score) for ch in first.values() for c in ch],
            [(c.doc_id, c.channel, c.raw_score) for ch in second.values() for c in ch],
        )

    def test_filters_are_applied(self):
        query = self.queries[0]
        caps = {name: 50 for name in
                ("inverted", "prefix", "category", "vector")}
        result = self.index.run_all(query, caps)
        for candidates in result.values():
            for candidate in candidates:
                doc = self.index.documents[candidate.doc_id]
                if query.category_filter is not None:
                    self.assertEqual(doc.category, query.category_filter)
                if query.min_quality is not None:
                    self.assertGreaterEqual(doc.quality_score, query.min_quality)


if __name__ == "__main__":
    unittest.main()
