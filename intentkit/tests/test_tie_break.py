import unittest

from intentkit.matcher import match_exact
from intentkit.models import IntentConfig, IntentRule, Ticket


def _config(threshold: float = 0.0) -> IntentConfig:
    labels = ("alpha_intent", "beta_intent")
    rules = [
        IntentRule(
            label="alpha_intent",
            keywords=("甲方",),
            weight=10,
            threshold=threshold,
            fallback_label=None,
            batch_size=8,
        ),
        IntentRule(
            label="beta_intent",
            keywords=("乙方",),
            weight=10,
            threshold=threshold,
            fallback_label=None,
            batch_size=8,
        ),
    ]
    return IntentConfig.build(
        version=1,
        controlled_labels=labels + ("general_consultation",),
        rules=rules
        + [
            IntentRule(
                label="general_consultation",
                keywords=("人工",),
                weight=1,
                threshold=0.0,
                fallback_label=None,
                batch_size=8,
            )
        ],
        default_fallback_label="general_consultation",
    )


class TieBreakTests(unittest.TestCase):
    def test_equal_score_breaks_by_label_lexicographic(self):
        config = _config()
        ticket = Ticket("T1", "甲方和乙方", "zh-CN", None)
        # 两个意图各命中一次、同长度同权重，分数完全相同，按标签字典序
        result = match_exact(ticket, config)
        self.assertEqual(result.intent_label, "alpha_intent")

    def test_longer_keyword_wins_on_tie(self):
        labels = ("alpha_intent", "beta_intent")
        rules = [
            IntentRule(
                label="alpha_intent",
                keywords=("ab",),
                weight=10,
                threshold=0.0,
                fallback_label=None,
                batch_size=8,
            ),
            IntentRule(
                label="beta_intent",
                keywords=("abc",),
                weight=10,
                threshold=0.0,
                fallback_label=None,
                batch_size=8,
            ),
            IntentRule(
                label="general_consultation",
                keywords=("人工",),
                weight=1,
                threshold=0.0,
                fallback_label=None,
                batch_size=8,
            ),
        ]
        config = IntentConfig.build(
            version=1,
            controlled_labels=labels + ("general_consultation",),
            rules=rules,
            default_fallback_label="general_consultation",
        )
        ticket = Ticket("T2", "abc", "zh-CN", None)
        result = match_exact(ticket, config)
        self.assertEqual(result.intent_label, "beta_intent")
        self.assertEqual(result.hits[0].keyword, "abc")


if __name__ == "__main__":
    unittest.main()
