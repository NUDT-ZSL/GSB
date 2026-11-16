"""流水线单元与集成测试（标准库 unittest，离线确定性运行）。"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zh_nlp.config import PipelineConfig  # noqa: E402
from zh_nlp.data import Lexicon, LogProbs  # noqa: E402
from zh_nlp.hmm import HmmIdentifier  # noqa: E402
from zh_nlp.numeric import stable_log_sum_exp  # noqa: E402
from zh_nlp.pipeline import (  # noqa: E402
    VERSION_MISMATCH_CODE, Pipeline, read_input)
from zh_nlp.pos_tagger import PosTagger  # noqa: E402
from zh_nlp.segmenter import MaximumMatchingSegmenter  # noqa: E402

SAMPLES = ROOT / "samples"


def base_config(tmp: Path, **overrides) -> PipelineConfig:
    config = PipelineConfig.from_file(SAMPLES / "config.json")
    data = config.to_dict()
    data.update(overrides)
    return PipelineConfig(**data)


class SegmentationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = base_config(self.tmp)
        self.lexicon = Lexicon(SAMPLES / "lexicon.txt")
        self.segmenter = MaximumMatchingSegmenter(self.lexicon, self.config)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_maximum_matching_positions(self):
        result = self.segmenter.segment("s02", "我喜欢自然语言处理")
        words = [token["word"] for token in result["tokens"]]
        self.assertEqual(words, ["我", "喜欢", "自然", "语言", "处理"])
        first = result["tokens"][1]
        self.assertEqual((first["start"], first["end"]), (1, 3))
        for token in result["tokens"]:
            self.assertTrue(token["in_lexicon"])
            self.assertGreater(token["boundary_confidence"], 0.0)
            self.assertLessEqual(token["boundary_confidence"], 1.0)

    def test_tie_break_frequency_then_lexicographic(self):
        lexicon_path = self.tmp / "lex.txt"
        lexicon_path.write_text("abc 5\nabd 5\nabe 2\n", encoding="utf-8")
        lexicon = Lexicon(lexicon_path)
        segmenter = MaximumMatchingSegmenter(
            lexicon, base_config(self.tmp, max_word_length=3))
        result = segmenter.segment("x", "abcx")
        # 等长且等频 -> 词形字典序升序，选 abc。
        self.assertEqual(result["tokens"][0]["word"], "abc")

    def test_frequency_priority_over_lexicographic(self):
        lexicon_path = self.tmp / "lex.txt"
        lexicon_path.write_text("abc 2\nabd 9\n", encoding="utf-8")
        lexicon = Lexicon(lexicon_path)
        segmenter = MaximumMatchingSegmenter(
            lexicon, base_config(self.tmp, max_word_length=3))
        result = segmenter.segment("x", "abd")
        self.assertEqual(result["tokens"][0]["word"], "abd")

    def test_unknown_single_char_confidence(self):
        result = self.segmenter.segment("x", "喵")
        token = result["tokens"][0]
        self.assertFalse(token["in_lexicon"])
        self.assertEqual(
            token["boundary_confidence"], self.config.unknown_confidence)


class HmmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = base_config(self.tmp)
        self.model = LogProbs(SAMPLES / "hmm_probs.json")
        self.hmm = HmmIdentifier(self.model, self.config)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_known_decode_bmes(self):
        result = self.hmm.decode(["猫", "咪"], [0, 1])
        self.assertEqual(result["tags"], ["B", "E"])
        self.assertEqual(len(result["tokens"]), 1)
        self.assertEqual(result["tokens"][0]["word"], "猫咪")
        self.assertEqual(result["smoothed_chars"], [])

    def test_missing_char_uses_smoothing_and_records_metadata(self):
        result = self.hmm.decode(["喵", "喵"], [0, 1])
        self.assertEqual(result["smoothed_chars"], ["喵", "喵"])
        expected = math.log(self.config.hmm_emission_smoothing)
        self.assertAlmostEqual(
            self.hmm.log_smoothing, expected, places=12)

    def test_log_domain_no_underflow(self):
        model = LogProbs(SAMPLES / "hmm_probs.json")
        self.assertTrue(np.issubdtype(model.transition.dtype, np.floating))
        result = self.hmm.decode(["猫"] * 50, list(range(50)))
        self.assertTrue(math.isfinite(result["viterbi_score"]))
        self.assertTrue(math.isfinite(
            result["log_normalized_probability"]))


class PosTaggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = base_config(self.tmp)
        self.model = LogProbs(SAMPLES / "pos_probs.json")
        self.tagger = PosTagger(self.model, self.config)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tokens(self, words):
        position = 0
        tokens = []
        for word in words:
            tokens.append({
                "word": word, "start": position,
                "end": position + len(word),
                "boundary_confidence": 1.0,
            })
            position += len(word)
        return tokens

    def test_tagging_outputs(self):
        result = self.tagger.tag(self._tokens(["我", "喜欢", "语言"]))
        self.assertEqual(result["tags"][0], "r")
        self.assertEqual(result["tags"][1], "v")
        self.assertEqual(len(result["tagged_tokens"]), 3)
        self.assertLessEqual(
            result["log_normalized_probability"],
            result["total_path_score"] + 1e-9)

    def test_low_confidence_threshold_recorded(self):
        config = base_config(self.tmp, low_confidence_threshold=0.0)
        tagger = PosTagger(self.model, config)
        result = tagger.tag(self._tokens(["我", "喜欢"]))
        self.assertTrue(
            all(token["low_confidence"] for token in result["tagged_tokens"]))
        self.assertEqual(
            result["tagged_tokens"][0]["triggered_threshold"], 0.0)

    def test_oov_word_smoothed(self):
        result = self.tagger.tag(self._tokens(["喵"]))
        self.assertEqual(result["smoothed_words"], ["喵"])
        self.assertTrue(math.isfinite(
            result["tagged_tokens"][0]["local_path_score"]))


class NumericStabilityTest(unittest.TestCase):
    def test_log_sum_exp_matches_direct_sum(self):
        values = np.array([-5.0, -6.0, -7.0])
        stable = stable_log_sum_exp(values)
        direct = math.log(sum(math.exp(value) for value in values))
        self.assertAlmostEqual(stable, direct, places=10)

    def test_extreme_negative_does_not_underflow(self):
        values = np.array([-1000.0, -1001.0, -1002.0])
        result = stable_log_sum_exp(values)
        self.assertTrue(math.isfinite(result))
        expected = -1000.0 + math.log(1.0 + math.exp(-1.0)
                                      + math.exp(-2.0))
        self.assertAlmostEqual(result, expected, places=9)

    def test_log_sum_exp_handles_neg_inf(self):
        values = np.array([float("-inf"), -1.0, float("-inf")])
        self.assertAlmostEqual(
            stable_log_sum_exp(values), -1.0, places=12)

    def test_log_sum_exp_empty(self):
        self.assertEqual(
            stable_log_sum_exp(np.array([])), float("-inf"))


class PipelineIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output = self.tmp / "out"
        self.config = base_config(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, config=None, output=None):
        pipeline = Pipeline(config or self.config, output or self.output)
        sentences = read_input(SAMPLES / "input.txt")
        return pipeline.run(sentences)

    def test_full_run_report_fields(self):
        code = self._run()
        self.assertEqual(code, 0)
        report = json.loads(
            (self.output / "report.json").read_text(encoding="utf-8"))
        for field in (
            "total_sentences", "succeeded_sentences", "failed_sentences",
            "stage_average_log_scores", "low_confidence_word_count",
            "config_hash", "stage_versions", "failures",
        ):
            self.assertIn(field, report)
        self.assertEqual(report["total_sentences"], 7)
        self.assertEqual(report["failed_sentences"], 0)
        self.assertEqual(
            set(report["stage_average_log_scores"]),
            {"segmentation", "hmm_oov", "pos_tagging"})

    def test_deterministic_byte_identical_outputs(self):
        self._run()
        second = self.tmp / "out2"
        self._run(output=second)
        for name in (
            "stage_segmentation.json", "stage_hmm_oov.json",
            "stage_pos_tagging.json", "report.json",
        ):
            self.assertEqual(
                (self.output / name).read_bytes(),
                (second / name).read_bytes())

    def test_resume_from_partial_stage_files(self):
        pipeline = Pipeline(self.config, self.output)
        pipeline.run(read_input(SAMPLES / "input.txt"))
        partial = self.tmp / "partial"
        partial.mkdir()
        shutil.copy(
            self.output / "stage_segmentation.json",
            partial / "stage_segmentation.json")
        resumed = Pipeline(self.config, partial)
        code = resumed.run(read_input(SAMPLES / "input.txt"))
        self.assertEqual(code, 0)
        for name in (
            "stage_hmm_oov.json", "stage_pos_tagging.json", "report.json",
        ):
            self.assertEqual(
                (self.output / name).read_bytes(),
                (partial / name).read_bytes())

    def test_version_mismatch_rejected(self):
        self._run()
        target = self.output / "stage_segmentation.json"
        data = json.loads(target.read_text(encoding="utf-8"))
        data["stage_version"] = 999
        target.write_text(
            json.dumps(data, ensure_ascii=False, sort_keys=True),
            encoding="utf-8")
        with self.assertRaises(SystemExit) as context:
            self._run()
        self.assertEqual(context.exception.code, 2)

    def test_failure_isolation_exit_code_one(self):
        bad_lexicon = self.tmp / "bad_lexicon.txt"
        bad_lexicon.write_text("我 1200\n坏行\n", encoding="utf-8")
        good = Lexicon(SAMPLES / "lexicon.txt")
        pipeline = Pipeline(self.config, self.output)

        original = pipeline.segmenter.segment

        def flaky(sentence_id, text):
            if sentence_id == "s03":
                raise RuntimeError("simulated per-sentence failure")
            return original(sentence_id, text)

        pipeline.segmenter.segment = flaky
        code = pipeline.run(read_input(SAMPLES / "input.txt"))
        self.assertEqual(code, 1)
        report = json.loads(
            (self.output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["failed_sentences"], 1)
        failure = report["failures"][0]
        self.assertEqual(failure["sentence_id"], "s03")
        self.assertEqual(failure["stage"], "segmentation")
        self.assertEqual(failure["exception_type"], "RuntimeError")
        self.assertEqual(report["succeeded_sentences"], 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
