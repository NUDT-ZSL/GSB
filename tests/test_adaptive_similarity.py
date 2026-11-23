from __future__ import annotations

import unittest

import numpy as np

from vocalforge.adaptive import ScaleObservation, choose_path
from vocalforge.alignment import align_approximate
from vocalforge.errors import AppError
from vocalforge.features import extract_dataset_features
from vocalforge.model import PrecisionConfig
from vocalforge.precision import NumericOps, stable_threshold_decision
from vocalforge.similarity import compare_paths
from vocalforge.synth import synthesize_dataset


class AdaptiveSimilarityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = synthesize_dataset()
        cls.features = extract_dataset_features(
            cls.dataset.utterances,
            cls.dataset.frame_ms,
            cls.dataset.frame_hop_ms,
            NumericOps("float64"),
        )

    def test_selector_is_deterministic_and_switches_on_scale(self) -> None:
        small = ScaleObservation(4, 460, 8, 64.0)
        self.assertEqual(choose_path(small), choose_path(small))
        self.assertEqual(choose_path(small)["decision"], "exact")
        large = ScaleObservation(24, 2_000_000, 64, 4.0)
        result = choose_path(large)
        self.assertEqual(result["decision"], "approximate")
        self.assertIn("total_frames_exceeds_exact_budget", result["reasons"])
        self.assertEqual(result["phoneme_coverage"]["missing"], [])

    def test_approximate_alignment_covers_all_controlled_phonemes(self) -> None:
        result = align_approximate(
            self.features,
            self.dataset.controlled_phonemes,
            self.dataset.phoneme_prototypes,
            coarse_hop_frames=3,
        )
        self.assertEqual(result.missing_phonemes, ())
        self.assertEqual(
            set(result.candidate_phonemes),
            set(self.dataset.controlled_phonemes),
        )

    def test_threshold_quantization_is_stable_near_boundary(self) -> None:
        threshold = 0.8
        for strategy in ("strict", "tolerance_band"):
            outcomes = []
            for profile in ("float64", "float32", "fixed"):
                config = PrecisionConfig(
                    profile=profile,
                    threshold=threshold,
                    comparison_strategy=strategy,
                    tolerance=1e-5,
                    decision_quantum=1e-5,
                )
                for value in (threshold - 1e-9, threshold, threshold + 1e-9):
                    outcomes.append(
                        stable_threshold_decision(value, config)["accepted"]
                    )
            self.assertEqual(len(set(outcomes)), 1)
            self.assertTrue(outcomes[0])

    def test_subnormal_guard_returns_zero_normalization(self) -> None:
        ops = NumericOps("float64", subnormal_floor=1e-12)
        normalized = ops.normalize([1e-320, -1e-320, 0.0])
        np.testing.assert_array_equal(normalized, np.zeros(3))

    def test_path_drift_error_when_similarity_incomparable(self) -> None:
        exact = {"utt": 0.95}
        approximate = {"utt": 0.20}
        with self.assertRaises(AppError) as context:
            compare_paths({}, {}, exact, approximate, PrecisionConfig())
        self.assertEqual(context.exception.code, "E_PATH_DRIFT")
        self.assertEqual(
            context.exception.details["source"],
            "similarity_agreement_below_minimum",
        )


if __name__ == "__main__":
    unittest.main()
