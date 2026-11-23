from __future__ import annotations

import math
import unittest

import numpy as np

from sceneopt.config import ThresholdConfig
from sceneopt.errors import (
    E_ERROR_SPECTRUM_INVALID,
    E_THRESHOLD_POLICY_INVALID,
    E_TOLERANCE_INVALID,
    ErrorSpectrumInvalid,
    ThresholdPolicyInvalid,
    ToleranceInvalid,
)
from sceneopt.spectrum import ErrorLevel, ErrorSpectrum, build_error_spectrum
from sceneopt.thresholds import evaluate_threshold, validate_tolerance


class SpectrumTests(unittest.TestCase):
    def _spectrum(self) -> ErrorSpectrum:
        return build_error_spectrum(
            registration_absolute_error=0.01,
            registration_error_interval=(0.001, 0.02),
            registration_scale=1.0,
            fusion_absolute_error=1e-15,
            fusion_error_interval=(0.0, 1e-15),
            simplification_absolute_error=0.02,
            simplification_error_interval=(0.0, 0.03),
            simplification_scale=2.0,
            texture_absolute_error=0.0,
            texture_error_interval=(0.0, 0.0),
            texture_scale=1.0,
        )

    def test_spectrum_orders_levels_and_attributes_dominant_source(self) -> None:
        spectrum = self._spectrum()
        self.assertEqual(
            [level.level for level in spectrum.levels],
            ["registration", "fusion_weights", "simplification", "texture_block"],
        )
        self.assertEqual(spectrum.dominant_level, "registration")
        self.assertTrue(
            all(
                level.error_interval[0] <= level.error_interval[1]
                for level in spectrum.levels
            )
        )

    def test_reversed_interval_is_rejected(self) -> None:
        with self.assertRaises(ErrorSpectrumInvalid) as context:
            ErrorLevel(
                level="registration",
                absolute_error=1.0,
                relative_error=1.0,
                error_interval=(2.0, 1.0),
                amplification_ratio=1.0,
                reference_scale=1.0,
            )
        self.assertEqual(context.exception.code, E_ERROR_SPECTRUM_INVALID)
        self.assertIn("error_interval", context.exception.details["field"])


class ThresholdTests(unittest.TestCase):
    def test_strict_and_tolerance_band_near_boundary_are_stable(self) -> None:
        limit = 0.05
        below = float(np.nextafter(limit, 0.0))
        above = float(np.nextafter(limit, math.inf))
        strict_below = evaluate_threshold(
            below, limit, policy="strict", direction="le"
        )
        strict_above = evaluate_threshold(
            above, limit, policy="strict", direction="le"
        )
        tolerant_below = evaluate_threshold(
            below,
            limit,
            policy="tolerance",
            tolerance_abs=1e-9,
            direction="le",
        )
        tolerant_above = evaluate_threshold(
            above,
            limit,
            policy="tolerance",
            tolerance_abs=1e-9,
            direction="le",
        )
        self.assertTrue(strict_below.passed)
        self.assertFalse(strict_above.passed)
        self.assertTrue(tolerant_below.passed)
        self.assertTrue(tolerant_above.passed)
        self.assertTrue(tolerant_below.within_tolerance)
        self.assertTrue(tolerant_above.within_tolerance)

    def test_subnormal_protection(self) -> None:
        subnormal = float(np.nextafter(0.0, 1.0))
        protected = evaluate_threshold(
            subnormal, 0.0, policy="strict", direction="le"
        )
        raw = evaluate_threshold(
            subnormal,
            0.0,
            policy="strict",
            direction="le",
            protect_subnormal=False,
        )
        self.assertTrue(protected.passed)
        self.assertTrue(protected.subnormal_clamped_metric)
        self.assertFalse(raw.passed)

    def test_invalid_policy_and_tolerance_have_required_codes(self) -> None:
        with self.assertRaises(ThresholdPolicyInvalid) as context:
            evaluate_threshold(1.0, 1.0, policy="fuzzy")
        self.assertEqual(context.exception.code, E_THRESHOLD_POLICY_INVALID)
        self.assertEqual(context.exception.details["field"], "threshold.policy")

        with self.assertRaises(ToleranceInvalid) as context:
            validate_tolerance(-1.0, "threshold.tolerance_abs")
        self.assertEqual(context.exception.code, E_TOLERANCE_INVALID)
        self.assertEqual(
            context.exception.details["field"], "threshold.tolerance_abs"
        )

        with self.assertRaises(ThresholdPolicyInvalid):
            ThresholdConfig(policy="unknown")
        with self.assertRaises(ToleranceInvalid):
            ThresholdConfig(absolute_tolerance=float("nan"))


if __name__ == "__main__":
    unittest.main()

