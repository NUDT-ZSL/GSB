from __future__ import annotations

import copy
import unittest

from dialogue_risk.aggregate import (
    aggregate_messages,
    verify_aggregation,
    weighted_mean,
)
from dialogue_risk.errors import AppError
from dialogue_risk.judge import judge_conversation
from dialogue_risk.normalize import normalize_text
from dialogue_risk.risk import (
    merge_risk_candidates,
    scan_risk_hits,
    validate_risk_span,
)
from dialogue_risk.sentiment import score_sentiment
from tests.helpers import canonical, load_fixture, single_message_conversation


class CoreJudgmentTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = load_fixture()
        self.config = fixture["config"]
        self.risk_words = fixture["risk_words"]
        self.conversations = fixture["conversations"]

    def test_normalization_and_sentiment_are_byte_stable(self) -> None:
        text = "  Ｉ  ＡＭ\u200b  ＮＯＴ   非常开心！！！ "
        normalized = normalize_text(text)
        self.assertEqual(normalized, "i am not 非常开心!!!")
        first = score_sentiment(normalized, self.config)
        second = score_sentiment(normalized, self.config)
        self.assertEqual(canonical(first), canonical(second))
        self.assertNotEqual(first["sentiment_label"], "positive")

    def test_negation_and_degree_modifiers(self) -> None:
        positive = score_sentiment(normalize_text("我非常满意"), self.config)
        negative = score_sentiment(normalize_text("我不太满意"), self.config)
        english = score_sentiment(normalize_text("I am not happy"), self.config)
        self.assertEqual(positive["sentiment_label"], "positive")
        self.assertEqual(negative["sentiment_label"], "negative")
        self.assertEqual(english["sentiment_label"], "negative")
        self.assertGreater(positive["sentiment_scores"]["positive"], 0.9)

    def test_label_tie_uses_fixed_order(self) -> None:
        config = copy.deepcopy(self.config)
        config["sentiment"]["lexicon"] = {"好": 0.5}
        config["sentiment"]["neutral_baseline"] = 0.5
        result = score_sentiment("好", config)
        self.assertEqual(result["sentiment_scores"]["neutral"], 0.5)
        self.assertEqual(result["sentiment_scores"]["positive"], 0.5)
        self.assertEqual(result["sentiment_label"], "neutral")

    def test_equal_score_message_tie_uses_ascending_msg_no(self) -> None:
        config = copy.deepcopy(self.config)
        config["aggregation"]["decay"]["enabled"] = False
        messages = []
        for msg_no in (2, 1):
            messages.append(
                {
                    "msg_no": msg_no,
                    "weight": 1.0,
                    "sentiment_scores": {
                        "negative": 0.0,
                        "neutral": 0.0,
                        "positive": 1.0,
                    },
                    "sentiment_score": 1.0,
                    "risk_hits": [],
                }
            )
        result = aggregate_messages(messages, config, self.risk_words)
        self.assertEqual(result["dominant_sentiment_msg_no"], 1)

    def test_risk_spans_are_inside_normalized_text_and_empty_hits_exist(self) -> None:
        result = judge_conversation(
            self.conversations["conv-0001"], self.config, self.risk_words, 0
        )
        for message in result["messages"]:
            for hit in message["risk_hits"]:
                self.assertGreaterEqual(hit["start"], 0)
                self.assertLess(hit["end"], len(message["normalized_text"]) + 1)
                self.assertEqual(
                    message["normalized_text"][hit["start"] : hit["end"]],
                    hit["term"],
                )
        empty = judge_conversation(
            single_message_conversation("请问订单什么时候送达"),
            self.config,
            self.risk_words,
            0,
        )
        self.assertEqual(empty["messages"][0]["risk_hits"], [])
        self.assertEqual(empty["risk_candidates"], [])

    def test_risk_span_validation_errors(self) -> None:
        normalized = normalize_text("请提供验证码")
        with self.assertRaises(AppError) as category_error:
            validate_risk_span(normalized, "illegal", 0, 1, self.risk_words)
        self.assertEqual(category_error.exception.code, "E_RISK_SPAN")
        with self.assertRaises(AppError) as range_error:
            validate_risk_span(
                normalized, "fraud", 0, len(normalized) + 1, self.risk_words
            )
        self.assertEqual(range_error.exception.code, "E_RISK_SPAN")

    def test_adjacent_same_category_candidates_merge(self) -> None:
        messages = [
            {
                "msg_no": 1,
                "risk_hits": [
                    {
                        "msg_no": 1,
                        "category": "fraud",
                        "start": 0,
                        "end": 1,
                        "term": "转账",
                        "severity": 0.8,
                    }
                ],
            },
            {
                "msg_no": 2,
                "risk_hits": [
                    {
                        "msg_no": 2,
                        "category": "fraud",
                        "start": 0,
                        "end": 1,
                        "term": "转账",
                        "severity": 0.8,
                    }
                ],
            },
            {"msg_no": 3, "risk_hits": []},
            {
                "msg_no": 4,
                "risk_hits": [
                    {
                        "msg_no": 4,
                        "category": "fraud",
                        "start": 0,
                        "end": 1,
                        "term": "转账",
                        "severity": 0.8,
                    }
                ],
            },
        ]
        candidates = merge_risk_candidates(messages, self.risk_words)
        self.assertEqual(len(candidates), 2)
        self.assertEqual((candidates[0]["msg_start"], candidates[0]["msg_end"]), (1, 2))
        self.assertEqual((candidates[1]["msg_start"], candidates[1]["msg_end"]), (4, 4))

    def test_aggregation_recomputes_and_detects_tampering(self) -> None:
        result = judge_conversation(
            self.conversations["conv-0002"], self.config, self.risk_words, 0
        )
        weights = [message["effective_weight"] for message in result["messages"]]
        expected_positive = weighted_mean(
            [
                message["sentiment_scores"]["positive"]
                for message in result["messages"]
            ],
            weights,
        )
        expected_negative = weighted_mean(
            [
                message["sentiment_scores"]["negative"]
                for message in result["messages"]
            ],
            weights,
        )
        expected = expected_positive - expected_negative
        self.assertAlmostEqual(expected, result["sentiment_score"], places=6)
        self.assertIn("formula", result["aggregation_diagnostics"]["decay"])
        self.assertIn(
            "risk_score", result["aggregation_diagnostics"]["risk_formula"]
        )
        tampered = copy.deepcopy(result)
        tampered["risk_score"] = min(1.0, tampered["risk_score"] + 0.2)
        with self.assertRaises(AppError) as error:
            verify_aggregation(tampered, self.config, self.risk_words)
        self.assertEqual(error.exception.code, "E_AGG_MISMATCH")
        message_tampered = copy.deepcopy(result)
        message_tampered["messages"][1]["effective_weight"] += 0.01
        with self.assertRaises(AppError) as message_error:
            verify_aggregation(message_tampered, self.config, self.risk_words)
        self.assertEqual(message_error.exception.code, "E_AGG_MISMATCH")
        self.assertEqual(message_error.exception.details["first_diff_msg_no"], 2)


if __name__ == "__main__":
    unittest.main()

