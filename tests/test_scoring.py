from __future__ import annotations

import unittest

from voice_dialog.audio import scan_audio_directory
from voice_dialog.constants import BALANCED_FEATURES, COARSE_EMOTIONS, FINE_EMOTIONS
from voice_dialog.features import extract_audio_features
from voice_dialog.scoring import choose_emotion, score_segment

from tests.helpers import TempCase


class ScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.case = TempCase()
        dataset = cls.case.dataset("scoring", segments=3, turns=1, anomalies=False)
        scan = scan_audio_directory(dataset / "audio")[0]
        cls.artifact = extract_audio_features(
            scan,
            frame_length_ms=25,
            frame_shift_ms=10,
            effective_features=BALANCED_FEATURES,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.case.close()

    def test_granularity_and_stable_normalization(self) -> None:
        coarse = score_segment(
            self.artifact,
            granularity="coarse",
            classifier="nearest_centroid",
            boundary_margin=0.03,
            reference_precision=0.05,
        )
        fine = score_segment(
            self.artifact,
            granularity="fine",
            classifier="nearest_centroid",
            boundary_margin=0.03,
            reference_precision=0.05,
        )
        self.assertEqual(tuple(coarse["categories"]), COARSE_EMOTIONS)
        self.assertEqual(tuple(fine["categories"]), FINE_EMOTIONS)
        self.assertAlmostEqual(sum(coarse["scores"].values()), 1.0, places=6)
        self.assertAlmostEqual(sum(fine["scores"].values()), 1.0, places=6)

    def test_classifier_switch_changes_real_path(self) -> None:
        centroid = score_segment(
            self.artifact,
            granularity="fine",
            classifier="nearest_centroid",
            boundary_margin=0.03,
            reference_precision=0.05,
        )
        logistic = score_segment(
            self.artifact,
            granularity="fine",
            classifier="weighted_logistic_regression",
            boundary_margin=0.03,
            reference_precision=0.05,
        )
        self.assertNotEqual(centroid["classifier_path"], logistic["classifier_path"])
        self.assertNotEqual(centroid["scores"], logistic["scores"])

    def test_boundary_margin_and_tie_rule(self) -> None:
        result = score_segment(
            self.artifact,
            granularity="fine",
            classifier="nearest_centroid",
            boundary_margin=1.0,
            reference_precision=1.0,
        )
        self.assertTrue(result["boundary"])
        self.assertAlmostEqual(
            result["score_margin"],
            result["ranked_scores"][0]["score"] - result["ranked_scores"][1]["score"],
            places=6,
        )
        self.assertEqual(choose_emotion({"beta": 0.5, "alpha": 0.5}), "alpha")


if __name__ == "__main__":
    unittest.main()
