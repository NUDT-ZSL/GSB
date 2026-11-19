from __future__ import annotations

import unittest
from pathlib import Path

from intent_interop.config_io import read_config_document
from intent_interop.errors import IntentError
from intent_interop.io_utils import read_samples
from intent_interop.matching import (
    ResourceLimits,
    match_document,
    self_check,
)
from intent_interop.models import Hit, MatchResult
from intent_interop.migration import prepare_for_matching
from intent_interop.pipeline import adjudicate

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class MatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.samples = read_samples(DATA / "queries.jsonl")
        cls.v2 = read_config_document(DATA / "config_v2.txt")
        cls.v3 = read_config_document(DATA / "config_v3.json")

    def test_cross_version_consistency(self) -> None:
        report, code = adjudicate(
            samples_path=DATA / "queries.jsonl",
            v1_path=DATA / "config_v1.txt",
            v2_path=DATA / "config_v2.txt",
            v3_path=DATA / "config_v3.json",
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(len(report["queries"]), 25)
        self.assertTrue(all(row["consistent"] for row in report["queries"]))

    def test_longest_then_label_lexicographic_tie_break(self) -> None:
        prepared = match_document(
            self.samples,
            prepare_for_matching(self.v3).document,
        )
        by_id = {row.query_id: row for row in prepared.results}
        row = by_id["q008"]
        self.assertEqual(row.intent, "cancel_order")
        self.assertEqual(
            [(hit.start, hit.end) for hit in row.hits],
            [(0, 4), (5, 9)],
        )
        self.assertEqual(row.hits[0].keyword, "申请退款")
        self.assertEqual(row.hits[1].keyword, "取消订单")

    def test_repeated_keyword_hits_are_non_overlapping(self) -> None:
        batch = match_document(
            self.samples,
            prepare_for_matching(self.v3).document,
        )
        row = {result.query_id: result for result in batch.results}["q009"]
        self.assertEqual([(hit.start, hit.end) for hit in row.hits], [(0, 2), (2, 4)])
        self.assertEqual(batch.self_check["direct_count"] + batch.self_check["fallback_count"], 25)

    def test_threshold_fallback(self) -> None:
        prepared = prepare_for_matching(self.v3).document
        intents = tuple(
            spec.with_changes(threshold=1.0) if spec.intent == "refund_request" else spec
            for spec in prepared.intents
        )
        high_threshold = prepared.with_changes(intents=intents)
        batch = match_document(self.samples, high_threshold)
        row = {result.query_id: result for result in batch.results}["q001"]
        self.assertEqual(row.intent, "fallback_unknown")
        self.assertEqual(row.fallback_category, "below_threshold")
        self.assertEqual(row.trigger_label, "refund_request")

    def test_retired_label_fallback_and_reject(self) -> None:
        shrunk = read_config_document(DATA / "config_v2_snapshot2_retired.txt")
        prepared = prepare_for_matching(shrunk).document
        fallback = match_document(self.samples, prepared, retired_policy="fallback")
        row = {result.query_id: result for result in fallback.results}["q001"]
        self.assertEqual(row.intent, "fallback_unknown")
        self.assertEqual(row.fallback_category, "label_retired")
        self.assertEqual(row.trigger_label, "refund_request")
        self.assertEqual(row.code, "E_LABEL_RETIRED")
        with self.assertRaises(IntentError) as raised:
            match_document(self.samples, prepared, retired_policy="reject")
        self.assertEqual(raised.exception.code, "E_LABEL_RETIRED")
        self.assertEqual(raised.exception.details["query_id"], "q001")

    def test_resource_limits_and_prefix_degrade(self) -> None:
        overflow = read_config_document(DATA / "config_v1_keyword_overflow.txt")
        prepared = prepare_for_matching(overflow).document
        with self.assertRaises(IntentError) as raised:
            match_document(self.samples, prepared, limits=ResourceLimits())
        self.assertEqual(raised.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(raised.exception.details["field"], "total_keywords")

        degraded = match_document(
            self.samples,
            prepared,
            limits=ResourceLimits(policy="prefix"),
        )
        self.assertIn("keyword_budget_prefix_only", degraded.degradation_categories)

        with self.assertRaises(IntentError) as long_raised:
            match_document(
                read_samples(DATA / "queries_overlong.jsonl"),
                prepare_for_matching(self.v2).document,
            )
        self.assertEqual(long_raised.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(long_raised.exception.details["query_id"], "q_long")

    def test_self_check_detects_overlap(self) -> None:
        sample = self.samples[0]
        fraudulent = MatchResult(
            query_id=sample.query_id,
            query_text=sample.query_text,
            locale=sample.locale,
            label_raw=sample.label_raw,
            matched_intent="refund_request",
            intent="refund_request",
            confidence=0.5,
            threshold=0.1,
            fallback_category=None,
            trigger_label=None,
            hits=(
                Hit("refund_request", "申请退款", 0, 4),
                Hit("refund_request", "退款", 2, 4),
            ),
        )
        with self.assertRaises(IntentError) as raised:
            self_check([sample], [fraudulent])
        self.assertEqual(raised.exception.code, "E_INCONSISTENT")
        self.assertEqual(raised.exception.details["category"], "offset_overlap")


if __name__ == "__main__":
    unittest.main()
