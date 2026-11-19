from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from timbre_lab.config import SimilarityConfig
from timbre_lab.decision import (
    CloneCandidateScore,
    rank_clone_scores,
)
from timbre_lab.diagnostics import (
    build_fault_injected_inputs,
    diagnose_dataset,
    diagnose_records,
)
from timbre_lab.errors import (
    DomainError,
    E_CONFIG_INVALID,
    E_DUPLICATE_UTTERANCE,
    E_FRAME_MISMATCH,
    E_PHONEME_OUT_OF_RANGE,
    E_REF_MISSING,
    E_SAMPLE_RATE_MISMATCH,
    E_SEED_SHAPE_MISMATCH,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
)
from timbre_lab.snapshots import SnapshotManager
from timbre_lab.synthesis import generate_dataset


class DecisionTests(unittest.TestCase):
    def test_similarity_config_validation_fields(self) -> None:
        cases = (
            (SimilarityConfig(threshold=1.5), "threshold"),
            (SimilarityConfig(tolerance=0.0), "tolerance"),
            (SimilarityConfig(top_k=0), "top_k"),
        )
        for config, field_name in cases:
            with self.assertRaises(DomainError) as context:
                config.validate()
            self.assertEqual(context.exception.code, E_CONFIG_INVALID)
            self.assertEqual(context.exception.field_name, field_name)

    def test_tolerance_band_and_strict_comparison_near_threshold(self) -> None:
        threshold = 0.7
        similarity = threshold - 0.4e-6
        strict = rank_clone_scores(
            [CloneCandidateScore("a", similarity, {})],
            SimilarityConfig(
                threshold=threshold,
                tolerance=1e-6,
                top_k=1,
                compare_strategy="strict",
            ),
        )
        tolerant = rank_clone_scores(
            [CloneCandidateScore("a", similarity, {})],
            SimilarityConfig(
                threshold=threshold,
                tolerance=1e-6,
                top_k=1,
                compare_strategy="tolerance",
            ),
        )
        self.assertFalse(strict["ranked"][0]["accepted"])
        self.assertTrue(tolerant["ranked"][0]["accepted"])
        self.assertTrue(tolerant["ranked"][0]["near_threshold"])

    def test_repeatability_and_deterministic_top_k_ties(self) -> None:
        config = SimilarityConfig(threshold=0.5, tolerance=1e-6, top_k=2)
        first = rank_clone_scores(
            [
                CloneCandidateScore("z", 0.8, {}),
                CloneCandidateScore("b", 0.9, {}),
                CloneCandidateScore("a", 0.9, {}),
            ],
            config,
        )
        second = rank_clone_scores(
            [
                CloneCandidateScore("a", 0.9, {}),
                CloneCandidateScore("b", 0.9, {}),
                CloneCandidateScore("z", 0.8, {}),
            ],
            config,
        )
        self.assertEqual(
            [item["candidate_id"] for item in first["top_k_candidates"]],
            [item["candidate_id"] for item in second["top_k_candidates"]],
        )
        self.assertEqual(
            [item["candidate_id"] for item in first["top_k_candidates"]],
            ["a", "b"],
        )
        for _ in range(20):
            repeated = rank_clone_scores(
                [
                    CloneCandidateScore("z", 0.8, {}),
                    CloneCandidateScore("b", 0.9, {}),
                    CloneCandidateScore("a", 0.9, {}),
                ],
                config,
            )
            self.assertEqual(repeated["ranked"], first["ranked"])

    def test_subnormal_guard_is_explicit(self) -> None:
        decision = rank_clone_scores(
            [CloneCandidateScore("tiny", 1e-310, {})],
            SimilarityConfig(
                threshold=0.0,
                tolerance=1e-9,
                top_k=1,
                compare_strategy="strict",
            ),
        )
        self.assertEqual(decision["subnormal_guard_count"], 1)
        self.assertEqual(decision["ranked"][0]["similarity_stable"], 0.0)
        self.assertFalse(decision["ranked"][0]["accepted"])


class SnapshotTests(unittest.TestCase):
    def test_monotonic_versions_rollback_and_wholesale_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = SnapshotManager(root)
            first.commit("embedding_stats", 1, {"speaker_count": 2, "values": [1, 2]})
            loaded = SnapshotManager(root)
            reloaded = loaded.reload()
            self.assertIn("embedding_stats", reloaded["changes"])
            first.commit("embedding_stats", 2, {"speaker_count": 3, "values": [3]})
            changed = loaded.reload()
            self.assertEqual(
                changed["changes"]["embedding_stats"]["change"],
                "version_changed",
            )
            with self.assertRaises(DomainError) as rollback:
                loaded.commit("embedding_stats", 1, {"speaker_count": 1})
            self.assertEqual(rollback.exception.code, E_SNAPSHOT_ROLLBACK)
            with self.assertRaises(DomainError) as conflict:
                loaded.commit("embedding_stats", 2, {"speaker_count": 99})
            self.assertEqual(conflict.exception.code, E_SNAPSHOT_ROLLBACK)

    def test_corrupt_checksum_reports_category_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = SnapshotManager(root)
            manager.commit("precision_config", 7, {"tier": "float64"})
            path = root / "precision_config.snapshot.json"
            wrapper = json.loads(path.read_text(encoding="utf-8"))
            wrapper["checksum"] = "0" * 64
            path.write_text(json.dumps(wrapper), encoding="utf-8")
            with self.assertRaises(DomainError) as corrupt:
                SnapshotManager(root).load("precision_config")
            error = corrupt.exception
            self.assertEqual(error.code, E_SNAPSHOT_CORRUPT)
            self.assertEqual(error.details["snapshot_category"], "precision_config")
            self.assertEqual(error.details["snapshot_version"], 7)


class DiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.dataset = generate_dataset(
            Path(cls.temp.name) / "diagnostics",
            seed=991,
            num_speakers=3,
            utterances_per_speaker=2,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_clean_dataset_has_no_findings(self) -> None:
        report = diagnose_dataset(self.dataset)
        self.assertTrue(report["ok"])
        self.assertEqual(report["business_codes"], [])
        self.assertEqual(report["unattributed_exceptions"], [])

    def test_fault_injection_covers_required_business_codes(self) -> None:
        entries, audios, truths = build_fault_injected_inputs(
            self.dataset,
            include_unattributed=True,
        )
        report = diagnose_records(
            entries,
            audios,
            truths,
            self.dataset.references,
            config=self.dataset.config,
        )
        expected_codes = {
            E_SAMPLE_RATE_MISMATCH,
            E_SEED_SHAPE_MISMATCH,
            E_REF_MISSING,
            E_DUPLICATE_UTTERANCE,
            E_PHONEME_OUT_OF_RANGE,
            E_FRAME_MISMATCH,
        }
        self.assertTrue(expected_codes.issubset(set(report["business_codes"])))
        self.assertFalse(report["ok"])
        self.assertTrue(report["unattributed_exceptions"])
        for finding in report["findings"]:
            self.assertIn("stage", finding)
            self.assertIn("utterance_id", finding)
            self.assertIn("code", finding)


if __name__ == "__main__":
    unittest.main()
