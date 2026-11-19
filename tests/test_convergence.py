from __future__ import annotations

import unittest

from numeric_workbench.convergence import decide_convergence
from tests.helpers import config_with


class ConvergenceStabilityTests(unittest.TestCase):
    def test_threshold_vicinity_is_stable_across_precision_epsilon(self):
        config = config_with(
            convergence_tolerance=1e-3,
            min_improvement=0.0,
            convergence_strategy="tolerance_band",
        )
        previous = 1.0
        threshold = 1e-3
        guard = 64.0 * 2.0**-23
        improvement = threshold + guard * 0.25
        current = previous - improvement
        outcomes = []
        for epsilon in (2.220446049250313e-16, 1.1920928955078125e-7, 0.0):
            decision = decide_convergence(
                3, previous, current, config, dtype_epsilon=epsilon
            )
            outcomes.append(decision.selected_converged)
            self.assertTrue(decision.strict_converged)
            self.assertTrue(decision.band_converged)
        self.assertEqual(outcomes, [True, True, True])

    def test_strict_and_band_retain_distinct_conclusions(self):
        config = config_with(
            convergence_tolerance=1e-3,
            min_improvement=0.0,
            convergence_strategy="tolerance_band",
        )
        previous = 1.0
        threshold = 1e-3
        improvement = threshold + 1e-4
        decision = decide_convergence(4, previous, previous - improvement, config)
        self.assertFalse(decision.strict_converged)
        self.assertTrue(decision.band_converged)
        self.assertTrue(decision.selected_converged)

    def test_subnormal_values_are_flushed(self):
        config = config_with(
            convergence_tolerance=1e-6,
            min_improvement=0.0,
            subnormal_floor=1e-300,
        )
        decision = decide_convergence(2, 5e-320, 0.0, config, dtype_epsilon=0.0)
        self.assertTrue(decision.subnormal_flushed)
        self.assertEqual(decision.improvement, 0.0)
        self.assertTrue(decision.selected_converged)


if __name__ == "__main__":
    unittest.main()
