from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from timbre_lab.config import PrecisionConfig
from timbre_lab.embedding import (
    candidate_catalog_and_bases,
    estimate_timbre,
    frame_alignment_vectors,
)
from timbre_lab.error_spectrum import compute_error_spectrum
from timbre_lab.errors import DomainError, E_CONFIG_INVALID
from timbre_lab.features import extract_and_validate, extract_features, f0_energy_observation
from timbre_lab.precision import (
    STAGE_NAMES,
    execute_precision,
    quantize_to_grid,
    verify_fixed_recomputation,
)
from timbre_lab.synthesis import generate_dataset


class PrecisionAndErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.dataset = generate_dataset(
            Path(cls.temp.name) / "data",
            seed=777,
            num_speakers=2,
            utterances_per_speaker=1,
        )
        estimates = {}
        for reference in cls.dataset.references.values():
            features = extract_features(
                reference.pcm,
                utterance_id=reference.ref_id,
            )
            estimates[reference.speaker_id] = estimate_timbre(
                features,
                owner_id=reference.ref_id,
                speaker_id=reference.speaker_id,
            )
        cls.catalog, cls.candidate_bases = candidate_catalog_and_bases(
            estimates.values(),
            8,
        )
        cls.sample = cls.dataset.utterances[0]
        cls.features = extract_and_validate(
            cls.sample.pcm,
            cls.sample.truth,
            utterance_id=cls.sample.utterance_id,
        )
        cls.frame_bases = frame_alignment_vectors(cls.features)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def _execute(self, tier: str, **overrides):
        config = PrecisionConfig(tier=tier, **overrides)
        return execute_precision(
            self.frame_bases,
            self.candidate_bases,
            self.catalog,
            config,
        )

    def test_precision_validation_fields(self) -> None:
        with self.assertRaises(DomainError) as unknown:
            PrecisionConfig(tier="float16").validate()
        self.assertEqual(unknown.exception.code, E_CONFIG_INVALID)
        self.assertEqual(unknown.exception.field_name, "precision_tier")
        with self.assertRaises(DomainError) as scale:
            PrecisionConfig(tier="fixed", fixed_scale=0).validate()
        self.assertEqual(scale.exception.code, E_CONFIG_INVALID)
        self.assertEqual(scale.exception.field_name, "fixed_scale")

    def test_all_tiers_preserve_frame_and_assignment_sets(self) -> None:
        executions = {
            tier: self._execute(tier) for tier in ("float64", "float32", "fixed")
        }
        reference = executions["float64"]
        for tier, execution in executions.items():
            self.assertEqual(execution.frame_set_digest, reference.frame_set_digest)
            self.assertEqual(execution.assignment_digest, reference.assignment_digest)
            self.assertEqual(execution.assignment_labels, reference.assignment_labels)
            self.assertEqual(execution.frame_indices, tuple(range(self.features.frame_count)))

    def test_each_tier_is_repeatable(self) -> None:
        for tier in ("float64", "float32", "fixed"):
            first = self._execute(tier)
            second = self._execute(tier)
            self.assertEqual(first.assignment_digest, second.assignment_digest)
            for stage in STAGE_NAMES:
                self.assertTrue(
                    np.array_equal(
                        first.stages[stage].frame_values,
                        second.stages[stage].frame_values,
                    )
                )

    def test_fixed_quantization_recomputes_from_scale_and_mode(self) -> None:
        execution = self._execute("fixed", fixed_scale=1 << 18, rounding_mode="half_even")
        verification = verify_fixed_recomputation(execution)
        self.assertTrue(verification["ok"])
        for stage_name in STAGE_NAMES:
            trace = execution.stages[stage_name]
            expected, _ = quantize_to_grid(
                trace.raw_frame_values,
                execution.config.fixed_scale,
                execution.config.rounding_mode,
            )
            self.assertTrue(np.array_equal(expected, trace.frame_values))

    def test_error_spectrum_has_full_propagation_contract(self) -> None:
        reference = self._execute("float64")
        observed = self._execute("fixed")
        truth = f0_energy_observation(self.features, self.sample.truth)
        spectrum = compute_error_spectrum(reference, observed, truth_observation=truth)
        self.assertEqual(set(spectrum["stages"]), set(STAGE_NAMES))
        for stage_name in STAGE_NAMES:
            stage = spectrum["stages"][stage_name]
            self.assertIn("abs_deviation_mean", stage)
            self.assertIn("relative_deviation", stage)
            self.assertLessEqual(stage["error_interval"]["low"], stage["error_interval"]["high"])
            self.assertGreaterEqual(stage["amplification_ratio"], 0.0)
            self.assertTrue(stage["details"])
            self.assertTrue(stage["detail_digest"])
            for row in stage["details"]:
                self.assertLessEqual(row["interval_low"], row["interval_high"])
        self.assertIn(
            spectrum["dominant_source"]["stage"],
            set(STAGE_NAMES),
        )
        self.assertTrue(spectrum["dominant_source"]["evidence_frames"])
        self.assertIn("f0_true_hz", spectrum["truth_observation"]["observations"])
        self.assertTrue(spectrum["fixed_recompute"]["ok"])


if __name__ == "__main__":
    unittest.main()

