from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from storybench.exercises import ExerciseGenerator
from storybench.generate import DEFAULT_SEED, build_data, generate_workspace, load_data
from storybench.model import MaskedDenoisingScorer
from storybench.pipeline import (
    build_pipeline,
    validate_pipeline,
    write_pipeline_outputs,
)
from storybench.plot import PlotChecker
from storybench.readability import IndependentReadabilityChecker
from storybench.rewrite import ConstrainedRewriter
from storybench.text import words


class WorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = build_data(DEFAULT_SEED)
        cls.pipeline_run = build_pipeline(cls.data)
        cls.validation = validate_pipeline(cls.data, cls.pipeline_run)

    def test_generation_is_deterministic_and_linked(self) -> None:
        again = build_data(DEFAULT_SEED)
        self.assertEqual(
            json.dumps(self.data, sort_keys=True),
            json.dumps(again, sort_keys=True),
        )
        self.assertEqual(6, len(self.data["stories"]))
        self.assertEqual(3, len(self.data["levels"]))
        for story in self.data["stories"]:
            self.assertEqual(len(story["sentences"]), len(story["events"]))
            for sentence, event in zip(story["sentences"], story["events"]):
                self.assertEqual(sentence["sentence_id"], event["sentence_id"])
                self.assertEqual(sentence["event_id"], event["event_id"])
                self.assertEqual(sentence["key_entities"], event["key_entities"])

    def test_generate_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = generate_workspace(temporary, DEFAULT_SEED)
            loaded = load_data(temporary)
            self.assertEqual(6, manifest["story_count"])
            self.assertEqual(
                json.dumps(self.data["stories"], sort_keys=True),
                json.dumps(loaded["stories"], sort_keys=True),
            )
            self.assertEqual(
                self.data["entity_aliases"], loaded["entity_aliases"]
            )

    def test_masked_denoiser_scores_every_position(self) -> None:
        scorer = MaskedDenoisingScorer(
            self.data["model_corpus"], self.data["lexicon"]
        )
        text = "Maya saw a big red fox."
        scores = scorer.score_candidate(text)
        self.assertEqual(len(words(text)), len(scores["positions"]))
        self.assertEqual(
            list(range(1, len(words(text)) + 1)),
            [position["word_ordinal"] for position in scores["positions"]],
        )
        for position in scores["positions"]:
            self.assertIn("[MASK]", position["masked_sentence"])
            self.assertGreater(position["probability"], 0)
            self.assertGreaterEqual(position["rank"], 1)
        self.assertGreater(scores["average_probability"], 0)

    def test_full_pipeline_passes_acceptance(self) -> None:
        self.assertTrue(self.validation["passed"], self.validation["errors"])
        summary = self.pipeline_run["summary"]
        self.assertEqual(summary["unit_count"], summary["approved_unit_count"])
        self.assertTrue(summary["readability"]["passed"])
        self.assertTrue(summary["plot"]["passed"])
        self.assertEqual(0, summary["plot"]["lost_entity_count"])
        self.assertEqual(0, summary["readability"]["violation_count"])
        self.assertTrue(summary["exercises"]["pairing_complete"])
        self.assertEqual(
            summary["exercises"]["cloze_count"],
            summary["exercises"]["wrong_word_count"],
        )
        self.assertGreater(summary["exercises"]["generated_exercise_count"], 0)

    def test_ledger_is_traceable_and_has_one_change_per_word(self) -> None:
        for row in self.pipeline_run["ledger"]:
            self.assertTrue(row["original_sentence"])
            self.assertTrue(row["rewritten_sentence"])
            ordinal_changes = [
                change["original_word_ordinal"]
                for change in row["replacement_basis"]
                if change["type"] in {"replace", "delete"}
            ]
            self.assertEqual(
                len(ordinal_changes), len(set(ordinal_changes)), row["ledger_id"]
            )
            for change in row["replacement_basis"]:
                self.assertTrue(change["rule_ref"])
                self.assertTrue(change["rubric_refs"])
            if row["replacement_basis"]:
                self.assertTrue(row["rubric_refs"])

    def test_cloze_and_wrong_word_are_paired_and_entity_safe(self) -> None:
        for exercise in self.pipeline_run["exercises"]:
            cloze = exercise["cloze"]
            wrong = exercise["wrong_word"]
            self.assertEqual(cloze["cloze_id"], exercise["pairing"]["cloze_id"])
            self.assertEqual(
                wrong["wrong_word_id"], exercise["pairing"]["wrong_word_id"]
            )
            self.assertEqual(cloze["position"], wrong["position"])
            self.assertIn("___", cloze["sentence"])
            self.assertTrue(cloze["not_key_entity"])
            surfaces = {entity.lower() for entity in exercise["target_key_entities"]}
            aliases = {
                key.lower(): [item.lower() for item in values]
                for key, values in self.data["entity_aliases"].items()
            }
            for entity in list(surfaces):
                surfaces.update(
                    alias.lower()
                    for alias in aliases.get(entity, [])
                )
            self.assertNotIn(cloze["answer"].lower(), surfaces)
            self.assertEqual(wrong["target_pos"], wrong["replacement_pos"])
            self.assertTrue(wrong["frame_match"])
            self.assertTrue(wrong["syntax_position_legal"])
            self.assertTrue(wrong["context_inappropriate"])
            self.assertGreater(
                wrong["model_evidence"]["original_log_probability"],
                wrong["model_evidence"]["replacement_log_probability"],
            )

    def test_plot_checker_reports_missing_entities(self) -> None:
        story = {
            "story_id": "test",
            "sentences": [
                {
                    "sentence_id": "S1",
                    "event_id": "E1",
                    "event_order": 1,
                    "key_entities": ["Maya", "fox"],
                },
                {
                    "sentence_id": "S2",
                    "event_id": "E2",
                    "event_order": 2,
                    "key_entities": ["Maya", "shop"],
                },
            ],
            "events": [
                {
                    "event_id": "E1",
                    "event_order": 1,
                    "sentence_id": "S1",
                    "key_entities": ["Maya", "fox"],
                },
                {
                    "event_id": "E2",
                    "event_order": 2,
                    "sentence_id": "S2",
                    "key_entities": ["Maya", "shop"],
                },
            ],
        }
        report = PlotChecker().check(
            story,
            "L1",
            {"S1": "Maya saw a bird.", "S2": "Maya walked to the shop."},
        )
        self.assertFalse(report["passed"])
        self.assertEqual(["fox"], report["units"][0]["lost_entities"])
        self.assertTrue(report["units"][0]["review_required"])

    def test_independent_readability_reports_both_constraints(self) -> None:
        level = copy.deepcopy(self.data["levels"][0])
        level["max_words"] = 3
        level["allowed_words"] = ["a", "fox", "maya", "saw"]
        checker = IndependentReadabilityChecker([level])
        report = checker.check("Maya saw a gigantic fox.", level["level_id"])
        constraints = {row["constraint"] for row in report["violations"]}
        self.assertEqual({"max_words", "vocabulary"}, constraints)
        self.assertFalse(report["passed"])

    def test_contextual_pos_resolves_fast_and_open(self) -> None:
        scorer = MaskedDenoisingScorer(
            self.data["model_corpus"], self.data["lexicon"]
        )
        generator = ExerciseGenerator(
            self.data["lexicon"], scorer, self.data["entity_aliases"]
        )
        self.assertEqual(
            "ADJ",
            generator._contextual_entry(
                "fast", ["a", "fast", "train"], 2
            )["pos"],
        )
        self.assertEqual(
            "ADV",
            generator._contextual_entry(
                "fast", ["ran", "fast", "across", "the", "road"], 2
            )["pos"],
        )
        self.assertEqual(
            "ADJ",
            generator._contextual_entry(
                "open", ["the", "open", "gate"], 2
            )["pos"],
        )

    def test_constraint_failure_falls_back_with_reasons(self) -> None:
        scorer = MaskedDenoisingScorer(
            self.data["model_corpus"], self.data["lexicon"]
        )
        level = {
            "level_id": "TEST",
            "max_words": 1,
            "allowed_words": ["the"],
            "synonyms": [],
            "modifier_policy": {"deletable_pos": [], "rule_ref": "TEST-MOD-DEL"},
            "split_policy": {
                "conjunctions": [],
                "min_words_per_part": 2,
                "require_right_clause_subject": True,
                "rule_ref": "TEST-SPLIT",
            },
            "constraint_refs": {"max_words": "TEST-LEN", "vocabulary": "TEST-VOCAB"},
        }
        rewriter = ConstrainedRewriter(level, self.data["lexicon"], scorer)
        result = rewriter.rewrite("Maya saw a giant fox.", "S1", [])
        self.assertEqual("fallback_original", result["status"])
        self.assertTrue(result["decoding"]["fallback_reasons"])

    def test_unchanged_original_when_no_safe_transform_exists(self) -> None:
        scorer = MaskedDenoisingScorer(
            self.data["model_corpus"], self.data["lexicon"]
        )
        level = next(
            copy.deepcopy(item)
            for item in self.data["levels"]
            if item["level_id"] == "L3"
        )
        rewriter = ConstrainedRewriter(level, self.data["lexicon"], scorer)
        result = rewriter.rewrite(
            "The worker thanked Omar and boarded the train.", "S1", ["worker", "Omar", "train"]
        )
        self.assertEqual("unchanged_original", result["status"])
        self.assertEqual(
            "The worker thanked Omar and boarded the train.", result["rewritten"]
        )

    def test_repeated_outputs_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = build_pipeline(self.data)
            second = build_pipeline(self.data)
            first_manifest = write_pipeline_outputs(first, first_dir)
            second_manifest = write_pipeline_outputs(second, second_dir)
            first_files = {
                path.relative_to(first_dir).as_posix(): path.read_bytes()
                for path in Path(first_dir).rglob("*")
                if path.is_file()
            }
            second_files = {
                path.relative_to(second_dir).as_posix(): path.read_bytes()
                for path in Path(second_dir).rglob("*")
                if path.is_file()
            }
            self.assertEqual(first_files, second_files)
            self.assertEqual(
                first_manifest["payload_sha256"],
                second_manifest["payload_sha256"],
            )
            self.assertEqual(
                first["summary"]["payload_sha256"],
                second["summary"]["payload_sha256"],
            )

    def test_stale_verification_artifact_is_removed_on_plain_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            write_pipeline_outputs(
                self.pipeline_run,
                temporary,
                {"checker": "repeatability-v1", "passed": True},
            )
            self.assertTrue((Path(temporary) / "verification.json").is_file())
            write_pipeline_outputs(self.pipeline_run, temporary)
            self.assertFalse((Path(temporary) / "verification.json").exists())


if __name__ == "__main__":
    unittest.main()
