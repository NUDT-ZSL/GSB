from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import generate_samples
from vocal_feedback.config import Config
from vocal_feedback.dataset import discover_samples
from vocal_feedback.evaluation import evaluate_sample
from vocal_feedback.pipeline import analyze_sample


class TestEndToEndAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        for plan in generate_samples.SAMPLE_PLANS:
            generate_samples.write_sample(cls.root, plan)
        cls.config = Config(
            frame_length_s=generate_samples.FRAME_LENGTH_S,
            hop_length_s=generate_samples.HOP_LENGTH_S,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_high_register_melisma_sample_passes_manual_checks(self) -> None:
        sample = next(
            item
            for item in discover_samples(self.root)
            if item.sample_id == "s02_high_melisma"
        )
        report = analyze_sample(sample, self.config)
        self.assertEqual(len(report["notes"]), 15)
        self.assertEqual(len(report["syllables"]), 14)
        self.assertTrue(report["mapping_validation"]["syllable_count_matches_text"])
        melisma = [
            note
            for note in report["notes"]
            if note["syllable_index"] == 6
        ]
        self.assertEqual(len(melisma), 2)
        primary = [note for note in melisma if note["mapping_role"] == "primary"]
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary[0]["id"], melisma[0]["id"])
        self.assertIsNotNone(sample.annotations)
        evaluation = evaluate_sample(report, sample.annotations, self.config)
        self.assertTrue(evaluation["passed"], evaluation)
        self.assertEqual(
            evaluation["frame_metrics"]["pitch_hit_rate_reference_voiced"], 1.0
        )
        self.assertLess(evaluation["note_metrics"]["boundary_mae_ms"], 100.0)
        self.assertLess(evaluation["phoneme_metrics"]["boundary_mae_ms"], 100.0)

    def test_raw_and_smoothed_pitch_are_both_present(self) -> None:
        sample = next(
            item
            for item in discover_samples(self.root)
            if item.sample_id == "s01_ballad"
        )
        report = analyze_sample(sample, self.config)
        voiced = [frame for frame in report["frames"] if frame["voiced"]]
        self.assertTrue(voiced)
        self.assertTrue(all("raw_f0_hz" in frame for frame in voiced))
        self.assertTrue(all("octave_corrected_f0_hz" in frame for frame in voiced))
        self.assertTrue(all(frame["f0_hz"] is not None for frame in voiced))
        self.assertTrue(all(frame["method"] in {"autocorrelation", "cepstrum"} for frame in voiced))


if __name__ == "__main__":
    unittest.main()

