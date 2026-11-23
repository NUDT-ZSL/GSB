from __future__ import annotations

import copy
import unittest

from vocalforge.alignment import alignment_digest
from vocalforge.errorprop import validate_error_spectrum
from vocalforge.errors import AppError
from vocalforge.features import extract_dataset_features
from vocalforge.precision import (
    NumericOps,
    validate_precision,
)
from vocalforge.model import PrecisionConfig
from vocalforge.service import LabService
from vocalforge.synth import synthesize_dataset
import tempfile


class PrecisionErrorPropagationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = LabService(tempfile.mkdtemp(prefix="vf-precision-"))

    def test_alignment_labels_are_invariant_across_profiles(self) -> None:
        digests = {
            profile: self.service._profiles[profile].alignment_digest
            for profile in ("float64", "float32", "fixed")
        }
        self.assertEqual(len(set(digests.values())), 1)
        self.assertTrue(self.service.precision_payload()["labels_invariant"])

    def test_invalid_profile_and_scale_are_structured(self) -> None:
        with self.assertRaises(AppError) as context:
            validate_precision(PrecisionConfig(profile="float16"))
        self.assertEqual(context.exception.code, "E_PROFILE_INVALID")
        self.assertEqual(context.exception.field, "profile")
        with self.assertRaises(AppError) as context:
            validate_precision(PrecisionConfig(profile="fixed", fixed_scale=0))
        self.assertEqual(context.exception.code, "E_PROFILE_INVALID")
        self.assertEqual(context.exception.field, "fixed_scale")
        with self.assertRaises(AppError) as context:
            validate_precision(
                PrecisionConfig(profile="fixed", rounding_mode="nearest")
            )
        self.assertEqual(context.exception.code, "E_PROFILE_INVALID")
        self.assertEqual(context.exception.field, "rounding_mode")

    def test_error_spectra_recompute_from_details(self) -> None:
        for profile in ("float64", "float32", "fixed"):
            result = validate_error_spectrum(self.service.error_spectrum(profile))
            self.assertTrue(result["ok"])
            self.assertEqual(result["levels_checked"][0], "feature_scaling")
        fixed = validate_error_spectrum(self.service.error_spectrum("fixed"))
        self.assertEqual(fixed["fixed_trace_checked"], 460)
        self.assertEqual(
            self.service.error_spectrum("float64")["dominant_source"], "none"
        )
        self.assertIn(
            self.service.error_spectrum("fixed")["dominant_source"],
            self.service.error_spectrum("fixed")["level_order"],
        )

    def test_reversed_interval_is_rejected(self) -> None:
        spectrum = copy.deepcopy(self.service.error_spectrum("fixed"))
        spectrum["levels"]["feature_scaling"]["intervals"][0]["start_frame"] = 999
        spectrum["levels"]["feature_scaling"]["intervals"][0]["end_frame"] = 1
        with self.assertRaises(AppError) as context:
            validate_error_spectrum(spectrum)
        self.assertEqual(context.exception.code, "E_ERRPROP_INCONSISTENT")
        self.assertIn("first_difference", context.exception.details)

    def test_corrupted_detail_cannot_be_recomputed(self) -> None:
        spectrum = copy.deepcopy(self.service.error_spectrum("fixed"))
        spectrum["levels"]["log_transform"]["details"][0]["abs_dev"] += 0.25
        with self.assertRaises(AppError) as context:
            validate_error_spectrum(spectrum)
        self.assertEqual(context.exception.code, "E_ERRPROP_INCONSISTENT")
        self.assertEqual(
            context.exception.details["first_difference"]["level"],
            "log_transform",
        )


if __name__ == "__main__":
    unittest.main()
