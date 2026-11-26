from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from dsn_workbench.errors import (
    E_COVERAGE_MISMATCH,
    E_TAG_INVALID,
    E_TOKEN_OVERLAP,
    WorkbenchError,
)
from dsn_workbench.normalization import normalize_sentence
from dsn_workbench.sample import default_config_data
from dsn_workbench.segmentation import disambiguate, generate_candidates
from dsn_workbench.tagging import tag_and_validate


class StageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = default_config_data()

    def _process(self, raw_text: str):
        normalized = normalize_sentence("s0001", raw_text, self.config)
        candidates = generate_candidates(normalized, self.config)
        tokens = disambiguate(normalized, candidates, self.config)
        tagged = tag_and_validate(normalized, tokens, self.config)
        return normalized, candidates, tokens, tagged

    def test_normalization_offsets_cover_original_code_points(self) -> None:
        raw = "Ａｂｃ  支付宝！\n"
        normalized = normalize_sentence("s0001", raw, self.config)
        self.assertEqual(normalized["normalized_text"], "abc 支付宝! ")
        char_map = normalized["char_map"]
        self.assertEqual(char_map[0][0], 0)
        self.assertEqual(char_map[-1][1], len(raw))
        for previous, current in zip(char_map, char_map[1:]):
            self.assertEqual(previous[1], current[0])

    def test_candidates_record_span_form_and_version(self) -> None:
        normalized = normalize_sentence(
            "s0001", "支付宝支付12.5%", self.config
        )
        candidates = generate_candidates(normalized, self.config)["candidates"]
        by_text = {}
        for candidate in candidates:
            by_text.setdefault(candidate["text"], candidate)
        self.assertEqual(by_text["支付宝"]["source"], "dictionary")
        self.assertEqual(
            by_text["支付宝"]["source_version"], "lex-2026-11-27.1"
        )
        self.assertEqual(by_text["支付"]["source"], "prefix_rule")
        self.assertEqual(
            by_text["支付"]["source_version"], "rules-2026-11-27.1"
        )
        self.assertEqual(by_text["12.5%"]["source"], "prefix_rule")
        for candidate in candidates:
            self.assertEqual(
                normalized["normalized_text"][
                    candidate["start"] : candidate["end"]
                ],
                candidate["text"],
            )

    def test_exact_dictionary_priority_beats_embedded_prefix_rule(self) -> None:
        normalized, _, tokens, _ = self._process("支付宝")
        self.assertEqual([token["text"] for token in tokens["tokens"]], ["支付宝"])
        self.assertEqual(tokens["tokens"][0]["source"], "dictionary")

    def test_priority_ties_prefer_shorter_form(self) -> None:
        config = copy.deepcopy(self.config)
        config["dictionary"].extend(
            [
                {
                    "entry_id": "lex-tie-1",
                    "term": "ab",
                    "tag": "NOUN",
                    "version": "lex-tie",
                    "domains": ["finance"],
                    "locales": ["zh-CN"],
                },
                {
                    "entry_id": "lex-tie-2",
                    "term": "abc",
                    "tag": "NOUN",
                    "version": "lex-tie",
                    "domains": ["finance"],
                    "locales": ["zh-CN"],
                },
            ]
        )
        normalized = normalize_sentence("s0001", "abc", config)
        candidates = generate_candidates(normalized, config)
        tokens = disambiguate(normalized, candidates, config)
        self.assertEqual([token["text"] for token in tokens["tokens"]], ["ab", "c"])
        self.assertEqual(tokens["tokens"][0]["source"], "dictionary")

    def test_uncovered_character_is_single_unknown_token(self) -> None:
        _, _, _, tagged = self._process("★")
        self.assertEqual(len(tagged["tokens"]), 1)
        token = tagged["tokens"][0]
        self.assertEqual(token["text"], "★")
        self.assertEqual(token["source"], "unknown")
        self.assertIn(token["pos_tag"], self.config["domain_tags"]["finance"])
        self.assertTrue(tagged["validation"]["normalized_coverage"])
        self.assertTrue(tagged["validation"]["raw_coverage"])

    def test_tagging_rejects_token_coverage_gap(self) -> None:
        normalized, _, tokens, _ = self._process("支付宝发布")
        broken = copy.deepcopy(tokens)
        broken["tokens"][1]["start"] += 1
        with self.assertRaises(WorkbenchError) as context:
            tag_and_validate(normalized, broken, self.config)
        self.assertEqual(context.exception.code, E_COVERAGE_MISMATCH)
        self.assertIn("sentence_id", context.exception.details)

    def test_tagging_rejects_overlap(self) -> None:
        normalized, _, tokens, _ = self._process("支付宝发布")
        broken = copy.deepcopy(tokens)
        broken["tokens"][1]["start"] = broken["tokens"][0]["start"]
        with self.assertRaises(WorkbenchError) as context:
            tag_and_validate(normalized, broken, self.config)
        self.assertEqual(context.exception.code, E_TOKEN_OVERLAP)

    def test_tagging_rejects_uncontrolled_hmm_output(self) -> None:
        normalized, _, tokens, _ = self._process("支付宝发布")
        with patch(
            "dsn_workbench.tagging._viterbi",
            return_value=["NOT_IN_CONTROLLED_SET"]
            * len(tokens["tokens"]),
        ):
            with self.assertRaises(WorkbenchError) as context:
                tag_and_validate(normalized, tokens, self.config)
        self.assertEqual(context.exception.code, E_TAG_INVALID)
        self.assertEqual(
            context.exception.details["controlled_tags"],
            sorted(set(self.config["domain_tags"]["finance"])),
        )

    def test_confidence_is_bounded_and_serializable(self) -> None:
        _, _, _, tagged = self._process("支付宝发布2025年财报。")
        for token in tagged["tokens"]:
            self.assertGreaterEqual(token["confidence"], 0.0)
            self.assertLessEqual(token["confidence"], 1.0)
            self.assertIsInstance(token["confidence"], float)


if __name__ == "__main__":
    unittest.main()

