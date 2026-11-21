from __future__ import annotations

import unittest

from query_understanding.metrics import evaluate_results, recompute_metrics
from query_understanding.slots import resolve_non_overlapping

from tests.common import load_engine, load_small


class SlotIntentMetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = load_engine()
        cls.queries = load_small()
        cls.results = cls.engine.process_many(cls.queries, path="exact")

    def test_all_spans_are_bounded_and_non_overlapping(self):
        for result in self.results:
            normalized = result["normalized_text"]
            occupied = []
            for slot in result["slots"]:
                self.assertLessEqual(0, slot["start"])
                self.assertLess(slot["start"], slot["end"])
                self.assertLessEqual(slot["end"], len(normalized))
                self.assertEqual(
                    normalized[slot["start"] : slot["end"]], slot["text"]
                )
                for start, end in occupied:
                    self.assertFalse(
                        slot["start"] < end and start < slot["end"]
                    )
                occupied.append((slot["start"], slot["end"]))
            self.assertTrue(result["self_check"]["ok"])

    def test_directional_city_slots(self):
        query = next(
            row
            for row in self.queries
            if "北京" in row["query_text"] and "深圳" in row["query_text"]
        )
        result = self.engine.process(query)
        slots = {slot["slot_name"]: slot for slot in result["slots"]}
        self.assertIn("city_from", slots)
        self.assertIn("city_to", slots)
        self.assertEqual(slots["city_from"]["text"], "北京")
        self.assertEqual(slots["city_to"]["text"], "深圳")

    def test_same_confidence_overlap_breaks_tie_by_slot_name(self):
        candidates = [
            {
                "slot_name": "zeta",
                "start": 0,
                "end": 3,
                "text": "abc",
                "normalized_value": "z",
                "confidence": 0.9,
                "match_method": "exact",
                "source_entry_id": "e1",
            },
            {
                "slot_name": "alpha",
                "start": 1,
                "end": 4,
                "text": "bcd",
                "normalized_value": "a",
                "confidence": 0.9,
                "match_method": "exact",
                "source_entry_id": "e2",
            },
        ]
        selected = resolve_non_overlapping(candidates)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["slot_name"], "alpha")

    def test_click_rerank_is_deterministic_and_keeps_ambiguous_intent(self):
        reranked = [
            result
            for result in self.results
            if result["disambiguation"]["rerank_applied"]
        ]
        self.assertTrue(reranked)
        for result in reranked[:10]:
            query = next(
                row
                for row in self.queries
                if row["query_id"] == result["query_id"]
            )
            target = query["raw_clicks"][0]
            self.assertEqual(result["domain"], target["domain"])
            self.assertEqual(result["intent_class"], target["intent_class"])

    def test_click_window_and_confidence_policy_are_recorded(self):
        result = self.results[0]
        self.assertEqual(
            result["disambiguation"]["click_window"]["rank_max"], 2
        )
        self.assertEqual(
            result["disambiguation"]["click_window"]["min_dwell_ms"], 300
        )
        self.assertEqual(
            result["disambiguation"]["confidence_policy"]["kind"], "softmax"
        )
        self.assertEqual(
            result["top_k_intents"][0]["confidence"],
            result["intent_confidence"],
        )
        self.assertEqual(
            result["domain_ranking"][0]["domain"], result["domain"]
        )
        self.assertAlmostEqual(
            result["domain_ranking"][0]["confidence"],
            result["domain_confidence"],
        )

    def test_metrics_recompute_exactly_from_details(self):
        metrics = evaluate_results(self.results, self.queries, self.engine.click_policy)
        recomputed = recompute_metrics(metrics)
        self.assertTrue(recomputed["consistent"])
        self.assertEqual(
            recomputed["aggregate"]["ndcg_at_k"],
            metrics["aggregate"]["ndcg_at_k"],
        )
        self.assertEqual(
            recomputed["aggregate"]["mrr"], metrics["aggregate"]["mrr"]
        )
        self.assertTrue(
            all(
                "relevance_by_intent" in detail
                and "dcg" in detail
                and "idcg" in detail
                for detail in metrics["details"]
            )
        )


if __name__ == "__main__":
    unittest.main()

