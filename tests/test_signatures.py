from __future__ import annotations

import unittest
from decimal import Decimal

from corpus_dedup_workbench.errors import (
    E_DOC_TOO_SHORT,
    E_SIG_LEN_INVALID,
    WorkbenchError,
)
from corpus_dedup_workbench.signatures import build_signature_record
from corpus_dedup_workbench.threshold import (
    stable_threshold_decision,
    subnormal_probe,
)
from corpus_dedup_workbench.util import canonical_json
from tests.helpers import sample_config


class SignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = sample_config()
        self.doc = {
            "shard_id": "shard-test",
            "doc_id": "doc-test",
            "text": " ".join(f"unit{index:03d}" for index in range(48)),
            "lang": "en",
            "source": "unit-test",
            "token_estimate": 48,
            "quality_score": 80.0,
        }

    def test_signature_and_summary_are_byte_deterministic(self) -> None:
        first = build_signature_record(self.doc, self.config)
        second = build_signature_record(self.doc, self.config)
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(first["block_signatures"], second["block_signatures"])
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(first["bucket_key"], second["bucket_key"])
        self.assertEqual(len(first["block_signatures"]), 4)
        self.assertTrue(
            all(len(signature) == 16 for signature in first["block_signatures"])
        )

    def test_rejects_non_positive_signature_length(self) -> None:
        config = sample_config(signature_length=0)
        with self.assertRaises(WorkbenchError) as caught:
            build_signature_record(self.doc, config)
        self.assertEqual(caught.exception.code, E_SIG_LEN_INVALID)

    def test_rejects_document_too_short_for_one_block(self) -> None:
        short_doc = {
            **self.doc,
            "text": "only two tokens",
            "token_estimate": 2,
        }
        with self.assertRaises(WorkbenchError) as caught:
            build_signature_record(short_doc, self.config)
        self.assertEqual(caught.exception.code, E_DOC_TOO_SHORT)


class ThresholdTests(unittest.TestCase):
    def test_exact_threshold_is_stable_in_both_policies(self) -> None:
        threshold = Decimal("0.75")
        tolerance = Decimal("0.000000001")
        strict = stable_threshold_decision(
            3, 4, threshold, "strict", tolerance
        )
        band = stable_threshold_decision(
            3, 4, threshold, "tolerance_band", tolerance
        )
        self.assertEqual(strict["score_fraction"], "3/4")
        self.assertTrue(strict["decision"])
        self.assertTrue(band["decision"])
        self.assertTrue(band["in_tolerance_band"])
        self.assertEqual(band["rule"], "exact_cross_product_in_tolerance_band")
        self.assertFalse(strict["subnormal_guarded"])

    def test_just_below_threshold_does_not_round_up(self) -> None:
        result = stable_threshold_decision(
            299,
            400,
            Decimal("0.75"),
            "tolerance_band",
            Decimal("0.000000001"),
        )
        self.assertFalse(result["decision"])
        self.assertEqual(result["score_fraction"], "299/400")

    def test_subnormal_guard_is_visible(self) -> None:
        result = stable_threshold_decision(
            1,
            10**310,
            Decimal("0.5"),
            "tolerance_band",
            Decimal("0.000000001"),
        )
        self.assertTrue(result["subnormal_guarded"])
        self.assertFalse(result["decision"])
        probe = subnormal_probe(score=5e-324, threshold=Decimal("0.5"))
        self.assertTrue(probe["guarded"])
        self.assertEqual(probe["effective_score"], 0.0)


if __name__ == "__main__":
    unittest.main()

