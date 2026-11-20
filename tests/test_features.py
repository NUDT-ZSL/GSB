import unittest

import numpy as np

from aed_scale.codes import BusinessCode
from aed_scale.config import FEATURE_NAMES_REDUCED, FeatureConfig
from aed_scale.errors import AedError
from aed_scale.features import extract_features, frame_count_for_samples


class FeatureTests(unittest.TestCase):
    def test_deterministic_bytes_and_sample_exact_frame_count(self):
        sample_rate = 16_000
        t = np.arange(8_005) / sample_rate
        pcm = 0.2 * np.sin(2 * np.pi * 440 * t) + 0.05 * np.cos(2 * np.pi * 90 * t)
        config = FeatureConfig()
        first = extract_features(pcm, config, dialog_id="dialog_a")
        second = extract_features(pcm, config, dialog_id="dialog_a")
        self.assertEqual(first.frame_count, frame_count_for_samples(len(pcm), config.hop_samples))
        self.assertEqual(first.frame_count, 51)
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(first.sha256(), second.sha256())
        self.assertEqual(first.frame_indices.tolist(), list(range(51)))
        self.assertEqual(first.features.shape, (51, 10))
        self.assertTrue(np.all(np.isfinite(first.features)))

    def test_reduced_mode_uses_core_features(self):
        config = FeatureConfig(feature_mode="reduced")
        result = extract_features(
            np.zeros(1_600),
            config,
            dialog_id="dialog_reduced",
        )
        self.assertEqual(result.feature_dimension, 4)
        self.assertEqual(result.config.feature_names, FEATURE_NAMES_REDUCED)

    def test_frame_mismatch_mentions_truth_count(self):
        with self.assertRaises(AedError) as context:
            extract_features(
                np.zeros(1_600),
                FeatureConfig(),
                dialog_id="dialog_mismatch",
                truth_frame_count=99,
            )
        error = context.exception
        self.assertEqual(error.code, BusinessCode.E_FRAME_MISMATCH.value)
        self.assertEqual(error.dialog_id, "dialog_mismatch")
        self.assertEqual(error.details["truth_frame_count"], 99)
        self.assertEqual(error.details["actual_frame_count"], 10)

    def test_invalid_configuration_fields(self):
        cases = [
            (FeatureConfig(sample_rate=8_000), "sample_rate"),
            (FeatureConfig(frame_ms=0.0), "frame_ms"),
            (FeatureConfig(frame_ms=10.0, hop_ms=20.0), "hop_ms"),
            (FeatureConfig(n_fft=256), "n_fft"),
        ]
        for config, field in cases:
            with self.subTest(field=field):
                with self.assertRaises(AedError) as context:
                    extract_features(np.zeros(100), config, dialog_id="dialog_bad")
                self.assertEqual(context.exception.code, BusinessCode.E_CONFIG_INVALID.value)
                self.assertEqual(context.exception.field, field)
                self.assertEqual(context.exception.dialog_id, "dialog_bad")


if __name__ == "__main__":
    unittest.main()
