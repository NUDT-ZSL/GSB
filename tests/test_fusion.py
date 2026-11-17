from __future__ import annotations

import unittest

from voice_dialog.dialogue import normalize_script, resolve_contexts
from voice_dialog.fusion import fuse_turn


def _score(emotion: str, categories: list[str], *, weight: float = 0.8) -> dict:
    other = (1.0 - weight) / (len(categories) - 1)
    scores = {category: other for category in categories}
    scores[emotion] = weight
    return {
        "categories": categories,
        "scores": scores,
        "predicted_emotion": emotion,
    }


class FusionTests(unittest.TestCase):
    def setUp(self) -> None:
        categories = ["angry", "happy", "neutral"]
        self.script = normalize_script(
            {
                "dialogue_id": "fusion",
                "turns": [
                    {"turn_id": "T1", "order": 1, "speaker": "A", "audio": "a.wav", "prev_turn": None, "intent": "greet"},
                    {"turn_id": "T2", "order": 2, "speaker": "A", "audio": "b.wav", "prev_turn": "T1", "intent": "inform"},
                    {"turn_id": "T3", "order": 3, "speaker": "B", "audio": "c.wav", "prev_turn": "T2", "intent": "request"},
                    {"turn_id": "T4", "order": 4, "speaker": "A", "audio": "d.wav", "prev_turn": "MISSING", "intent": "confirm"},
                ],
            }
        )
        self.categories = categories
        self.scores = {
            "a.wav": _score("happy", categories),
            "b.wav": _score("happy", categories, weight=0.7),
            "c.wav": _score("neutral", categories, weight=0.6),
            "d.wav": _score("angry", categories, weight=0.65),
        }

    def test_weights_and_contributions(self) -> None:
        contexts = resolve_contexts(self.script, window=2)
        context = contexts[2]
        entries = [{"turn": turn, "score": self.scores[turn["audio"]]} for turn in context["chain"]]
        fused = fuse_turn(
            context["turn"],
            entries,
            decay=0.5,
            same_speaker_weight=1.0,
            cross_speaker_weight=0.4,
            fallback_source=context["fallback_source"],
            boundary_margin=0.02,
        )
        self.assertEqual(context["predecessor_ids"], ["T2", "T1"])
        self.assertEqual([item["speaker_relation"] for item in fused["context_contributions"]], ["current", "cross_speaker", "cross_speaker"])
        self.assertEqual(fused["context_contributions"][1]["speaker_weight"], 0.4)
        self.assertAlmostEqual(sum(item["normalized_share"] for item in fused["context_contributions"]), 1.0, places=6)
        self.assertAlmostEqual(sum(fused["fused_scores"].values()), 1.0, places=6)

    def test_missing_predecessor_fallback(self) -> None:
        contexts = resolve_contexts(self.script, window=2, rule="nearest_available")
        self.assertTrue(contexts[3]["missing_predecessor"])
        self.assertEqual(contexts[3]["chain"][1]["turn_id"], "T3")
        self.assertIn("nearest_available:T3", contexts[3]["fallback_source"])
        zero_context = resolve_contexts(self.script, window=2, rule="zero_context")
        self.assertEqual(zero_context[3]["predecessor_ids"], [])
        self.assertIn("zero_context", zero_context[3]["fallback_source"])

    def test_injected_missing_predecessor(self) -> None:
        contexts = resolve_contexts(self.script, window=3, forced_missing_targets={"T2"})
        self.assertTrue(contexts[1]["missing_predecessor"])
        self.assertIn("injected_missing_predecessor", contexts[1]["fallback_source"])
        self.assertEqual(contexts[1]["chain"][1]["turn_id"], "T1")


if __name__ == "__main__":
    unittest.main()
