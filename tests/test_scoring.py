from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from ticket_risk.loader import Sample, load_inputs
from ticket_risk.scoring import (
    fusion_stage,
    normalize_stage,
    normalize_text,
    result_from_artifacts,
    risk_stage,
    sentiment_stage,
)


ROOT = Path(__file__).resolve().parents[1]


class ScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_inputs(
            ROOT / "data/samples.jsonl",
            ROOT / "resources/rules.json",
            ROOT / "config/determination.json",
        )

    def test_normalization(self) -> None:
        self.assertEqual(normalize_text("  ＡＢＣ，  你好！！ "), "abc 你好")
        self.assertEqual(normalize_text("Ａ\u3000Ｂ\tＣ"), "a b c")

    def test_degree_word_is_not_a_negation(self) -> None:
        sample = Sample("X1", "我特别满意这个结果", "在线客服", 1)
        normalized = normalize_stage(sample, self.bundle.rules, self.bundle.config)
        result = sentiment_stage(sample, normalized, self.bundle.rules, self.bundle.config)
        self.assertGreater(result["normalized_score"], 0)
        self.assertEqual(result["polarity"], "positive")
        self.assertEqual(result["contributions"][0]["negation_words"], [])
        self.assertEqual(result["contributions"][0]["degree_word"], "特别")

    def test_negation_window_flips_polarity(self) -> None:
        sample = Sample("X2", "我没有解决这个问题", "在线客服", 1)
        normalized = normalize_stage(sample, self.bundle.rules, self.bundle.config)
        result = sentiment_stage(sample, normalized, self.bundle.rules, self.bundle.config)
        contribution = next(item for item in result["contributions"] if item["word"] == "解决")
        self.assertTrue(contribution["negated"])
        self.assertLess(result["normalized_score"], 0)

    def test_dominant_word_tie_uses_dictionary_order(self) -> None:
        rules = copy.deepcopy(self.bundle.rules)
        config = copy.deepcopy(self.bundle.config)
        config["position_decay"] = 0.0
        config["negation_window"] = 0
        rules["sentiment_lexicon"] = {
            "乙": {"weight": 0.5},
            "甲": {"weight": 0.5},
        }
        rules["degree_adverbs"] = {}
        rules["negation_words"] = []
        sample = Sample("X3", "乙甲", "在线客服", 1)
        normalized = normalize_stage(sample, rules, config)
        result = sentiment_stage(sample, normalized, rules, config)
        self.assertEqual(result["dominant_word"], "乙")
        self.assertEqual([item["word"] for item in result["contributions"]], ["乙", "甲"])

    def test_repeated_runs_are_field_identical(self) -> None:
        sample = self.bundle.samples[0]
        artifacts_a = {}
        artifacts_b = {}
        for target in (artifacts_a, artifacts_b):
            target["normalize"] = normalize_stage(sample, self.bundle.rules, self.bundle.config)
            target["sentiment"] = sentiment_stage(
                sample, target["normalize"], self.bundle.rules, self.bundle.config
            )
            target["risk"] = risk_stage(
                sample, target["normalize"], self.bundle.rules, self.bundle.config
            )
            target["fusion"] = fusion_stage(
                sample,
                target["sentiment"],
                target["risk"],
                self.bundle.rules,
                self.bundle.config,
            )
        first = json.dumps(result_from_artifacts(sample, artifacts_a), ensure_ascii=False, sort_keys=True)
        second = json.dumps(result_from_artifacts(sample, artifacts_b), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_fusion_is_finite_and_uses_stable_softmax(self) -> None:
        sample = self.bundle.samples[0]
        normalized = normalize_stage(sample, self.bundle.rules, self.bundle.config)
        sentiment = sentiment_stage(sample, normalized, self.bundle.rules, self.bundle.config)
        risk = risk_stage(sample, normalized, self.bundle.rules, self.bundle.config)
        fusion = fusion_stage(sample, sentiment, risk, self.bundle.rules, self.bundle.config)
        self.assertTrue(0 <= fusion["confidence"] <= 1)
        self.assertAlmostEqual(sum(fusion["softmax_weights"].values()), 1.0, places=10)

    def test_conflict_table_resolves_positive_tone_high_risk(self) -> None:
        sample = Sample("X4", "起诉，但我很满意", "在线客服", 1)
        normalized = normalize_stage(sample, self.bundle.rules, self.bundle.config)
        sentiment = sentiment_stage(sample, normalized, self.bundle.rules, self.bundle.config)
        risk = risk_stage(sample, normalized, self.bundle.rules, self.bundle.config)
        fusion = fusion_stage(sample, sentiment, risk, self.bundle.rules, self.bundle.config)
        self.assertEqual(risk["base_risk_level"], "风险")
        self.assertEqual(sentiment["polarity"], "positive")
        self.assertTrue(fusion["conflict"]["matched"])
        self.assertEqual(fusion["conflict"]["winner"], "risk")
        self.assertEqual(fusion["final_risk_level"], "风险")


if __name__ == "__main__":
    unittest.main()
