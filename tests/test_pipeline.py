from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import numpy as np

from domain_vocab.cli import main
from domain_vocab.extension import (
    build_all_version_analysis,
    build_extension,
    consistency_audit,
)
from domain_vocab.generator import generate_data
from domain_vocab.normalization import (
    VERSIONS,
    compare_version_segmentations,
    normalize_identifier,
    render_identifier,
)
from domain_vocab.pipeline import PipelineConfig, run_pipeline


class NormalizationTests(unittest.TestCase):
    def test_regular_identifier_is_equivalent_across_versions(self) -> None:
        segments = ("mp", "1042", "3401", "a")
        normalized = {
            version: normalize_identifier(render_identifier(segments, version), version)
            for version in VERSIONS
        }
        for version in VERSIONS:
            self.assertEqual(normalized[version].segments, segments)
            self.assertEqual(normalized[version].token, "mp::1042::3401::a")
        comparison = compare_version_segmentations(normalized)
        self.assertFalse(comparison["has_difference"])

    def test_v3_merges_and_recovers_consecutive_four_digit_atoms(self) -> None:
        raw = render_identifier(("tar", "1042", "5501", "peak"), "v3")
        self.assertEqual(raw, "tar.10425501.peak")
        normalized = normalize_identifier(raw, "v3")
        self.assertEqual(normalized.segments, ("tar", "1042", "5501", "peak"))

    def test_delimiter_and_numeric_merge_conflicts_are_detected(self) -> None:
        delimiter_case = {
            version: normalize_identifier(
                render_identifier(("wx", "rain-snow", "3401"), version), version
            )
            for version in VERSIONS
        }
        delimiter_diff = compare_version_segmentations(delimiter_case)
        self.assertTrue(delimiter_diff["token_set_difference"])
        self.assertIn("delimiter_ambiguity", delimiter_diff["reasons"])

        numeric_case = {
            version: normalize_identifier(
                render_identifier(("mp", "12", "345", "a"), version), version
            )
            for version in VERSIONS
        }
        numeric_diff = compare_version_segmentations(numeric_case)
        self.assertIn("numeric_merge_ambiguity", numeric_diff["reasons"])


class ExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = generate_data(
            seed=202411, samples=168, embedding_dim=24, primary_version="v1"
        )
        cls.builds = {
            version: build_extension(cls.data, version) for version in VERSIONS
        }

    def test_admission_rule_and_fallbacks(self) -> None:
        build = self.builds["v1"]
        by_key = {candidate.entity.key: candidate for candidate in build.candidates}
        self.assertFalse(by_key["e015"].accepted)
        self.assertFalse(by_key["e016"].accepted)
        self.assertFalse(by_key["e017"].accepted)
        self.assertEqual(by_key["e015"].overlap_count, 1)
        self.assertEqual(by_key["e016"].overlap_count, 0)
        self.assertEqual(by_key["e017"].overlap_count, 1)
        self.assertEqual(by_key["e018"].decision_rule, "corpus_frequency>=3")
        self.assertTrue(by_key["e018"].accepted)

    def test_global_mean_initialization_when_no_shared_affix(self) -> None:
        by_key = {
            candidate.entity.key: candidate for candidate in self.builds["v1"].candidates
        }
        candidate = by_key["e018"]
        assert candidate.initialization is not None
        self.assertEqual(candidate.initialization.referenced_token_ids, [])
        self.assertEqual(
            candidate.initialization.method,
            "original_embedding_matrix_global_mean",
        )
        np.testing.assert_array_equal(
            candidate.initialization.embedding,
            self.data.base_embeddings.mean(axis=0),
        )

    def test_non_conflicting_versions_share_tokens_and_initial_embeddings(self) -> None:
        for entity_key in ("e001", "e005", "e011", "e019", "e020", "e021"):
            candidates = [
                next(
                    candidate
                    for candidate in self.builds[version].candidates
                    if candidate.entity.key == entity_key
                )
                for version in VERSIONS
            ]
            if entity_key in {"e019", "e020"}:
                self.assertGreater(len({candidate.whole_token for candidate in candidates}), 1)
                continue
            self.assertEqual(len({candidate.whole_token for candidate in candidates}), 1)
            initializations = [
                candidate.initialization.embedding
                for candidate in candidates
                if candidate.initialization is not None
            ]
            for embedding in initializations[1:]:
                np.testing.assert_array_equal(initializations[0], embedding)

    def test_conflict_audit_reports_both_known_cases(self) -> None:
        analysis = build_all_version_analysis(self.data)
        conflicts = {entry["identifier_key"]: entry for entry in analysis["conflicts"]}
        self.assertIn("e019", conflicts)
        self.assertIn("e020", conflicts)
        self.assertIn("delimiter_ambiguity", conflicts["e019"]["reasons"])
        self.assertIn("numeric_merge_ambiguity", conflicts["e020"]["reasons"])
        self.assertTrue(conflicts["e019"]["suggested_review"])

    def test_consistency_audit_is_independent_and_reports_outlier(self) -> None:
        audit = consistency_audit(
            self.builds["v1"], self.data.base_embeddings, threshold=0.6
        )
        self.assertEqual(audit["checked_token_count"], len(self.builds["v1"].accepted))
        self.assertFalse(audit["passed"])
        self.assertTrue(
            any("zq::high::0000::z" == item["new_token"] for item in audit["anomalies"])
        )


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = PipelineConfig(
            seed=202411,
            vocab_version="all",
            samples=168,
            embedding_dim=24,
            epochs=50,
            learning_rate=0.03,
        )
        cls.result = run_pipeline(cls.config)

    def test_frozen_rows_and_nonzero_new_row_updates(self) -> None:
        report = self.result["adaptation_report"]
        self.assertTrue(report["frozen_verification"]["original_rows_unchanged"])
        self.assertEqual(report["frozen_verification"]["max_abs_change"], 0.0)
        self.assertEqual(len(report["epoch_losses"]), 50)
        stats = report["new_token_embedding_update_stats"]
        self.assertGreater(stats["minimum"], 0.0)
        self.assertGreater(stats["mean"], 0.0)
        ranges = report["parameter_index_ranges"]
        base_size = self.result["dataset"]["base_vocabulary_size"]
        self.assertEqual(ranges["embedding_rows_half_open"][0], base_size)

    def test_loss_direction_and_domain_hit_rate(self) -> None:
        comparison = self.result["downstream_comparison"]
        self.assertLess(comparison["loss_after"], comparison["loss_before"])
        self.assertGreater(comparison["loss_gain"], 0.0)
        self.assertEqual(comparison["loss_direction"], "improved")
        self.assertGreaterEqual(comparison["hit_rate_after"], comparison["hit_rate_before"])
        self.assertGreater(comparison["hit_rate_after"], 0.0)
        self.assertTrue(self.result["validation"]["loss_direction_correct"])

    def test_low_support_direction_is_marked_uncertain(self) -> None:
        result = run_pipeline(
            PipelineConfig(
                seed=11,
                vocab_version="v1",
                samples=32,
                embedding_dim=16,
                epochs=8,
                learning_rate=0.03,
            )
        )
        comparison = result["downstream_comparison"]
        self.assertTrue(comparison["low_support_identifiers"])
        self.assertTrue(comparison["uncertain_identifiers"])
        self.assertTrue(
            any(
                "少于 2 条" in item["reason"]
                for item in comparison["uncertain_identifiers"]
            )
        )

    def test_result_is_deterministic(self) -> None:
        second = run_pipeline(self.config)
        self.assertEqual(
            json.dumps(self.result, sort_keys=True, ensure_ascii=False),
            json.dumps(second, sort_keys=True, ensure_ascii=False),
        )

    def test_cli_writes_identical_json_to_file_and_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "result.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--seed",
                        "77",
                        "--samples",
                        "64",
                        "--epochs",
                        "8",
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            printed = json.loads(stdout.getvalue())
            written = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(printed, written)
            self.assertEqual(printed["config"]["seed"], 77)

    def test_module_entrypoint_is_available(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "domain_vocab",
                "--seed",
                "5",
                "--samples",
                "32",
                "--epochs",
                "4",
                "--compact",
            ],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        )
        parsed = json.loads(completed.stdout)
        self.assertEqual(parsed["config"]["seed"], 5)


if __name__ == "__main__":
    unittest.main()

