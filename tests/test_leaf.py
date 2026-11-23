import unittest
from dataclasses import replace

import numpy as np

from gbtdiag import TrainConfig
from gbtdiag.errors import DomainError, ErrorCode
from gbtdiag.leaf import solve_leaf


class LeafSolverTests(unittest.TestCase):
    def setUp(self):
        self.config = TrainConfig(seed=3, hessian_floor=2.0)
        self.indices = np.arange(10)

    def test_floor_protection_flagged_in_report(self):
        gradients = np.linspace(-1.0, 1.0, 10)
        hessians = np.full(10, 0.05)
        result = solve_leaf(
            gradients, hessians, self.indices, self.config, "root.L"
        )
        self.assertTrue(result.floor_protected)
        self.assertEqual(result.hess_sum, 2.0)
        expected = -float(np.sum(gradients)) / 2.0
        self.assertAlmostEqual(result.weight, expected, places=12)

    def test_no_protection_when_denominator_healthy(self):
        gradients = np.linspace(-1.0, 1.0, 10)
        hessians = np.full(10, 1.0)
        result = solve_leaf(
            gradients, hessians, self.indices, self.config, "root"
        )
        self.assertFalse(result.floor_protected)

    def test_invalid_floor_returns_config_error_with_path(self):
        bad = replace(self.config, hessian_floor=-0.5)
        with self.assertRaises(DomainError) as context:
            solve_leaf(
                np.zeros(4),
                np.ones(4),
                np.arange(4),
                bad,
                "root.R.L",
            )
        self.assertEqual(context.exception.code, ErrorCode.CONFIG_INVALID)
        self.assertEqual(context.exception.field, "hessian_floor")
        self.assertEqual(context.exception.node_path, "root.R.L")

    def test_nonfinite_hessian_degrades_to_default_floor(self):
        gradients = np.ones(6)
        hessians = np.ones(6)
        hessians[[1, 4]] = np.nan
        config = replace(self.config, hessian_floor_default=3.0)
        result = solve_leaf(
            gradients, hessians, self.indices[:6], config, "root"
        )
        self.assertTrue(result.nonfinite_hessian)
        self.assertEqual(
            result.records[0]["code"],
            ErrorCode.NONFINITE_HESSIAN.value,
        )
        self.assertEqual(
            result.records[0]["degradation"]["category"],
            "default_floor_fallback",
        )
        self.assertEqual(result.records[0]["sample_indices"], [1, 4])

    def test_rounding_modes_are_exercised_in_quantization(self):
        from gbtdiag.precision import quantize, round_array

        # half_even rounds ties to even; half_up/half_down/trunc differ.
        values = np.array([0.5, 1.5, 2.5, -0.5, -1.5])
        self.assertTrue(
            np.array_equal(
                round_array(values, "half_even"),
                np.array([0.0, 2.0, 2.0, -0.0, -2.0]),
            )
        )
        self.assertTrue(
            np.array_equal(
                round_array(values, "half_up"),
                np.array([1.0, 2.0, 3.0, -0.0, -1.0]),
            )
        )
        self.assertTrue(
            np.array_equal(
                round_array(values, "trunc"),
                np.array([0.0, 1.0, 2.0, -0.0, -1.0]),
            )
        )
        config = replace(self.config, precision="float32")
        quantised = quantize(np.array([1.0 / 3.0]), config)
        self.assertEqual(quantised.dtype, np.float64)
        self.assertNotEqual(float(quantised[0]), 1.0 / 3.0)

    def test_subnormal_protection_flushes_tiny_weights(self):
        from gbtdiag.precision import quantize

        tiny = np.finfo(np.float32).smallest_subnormal * 0.75
        protected = replace(
            self.config,
            precision="float32",
            subnormal_protection=True,
        )
        unprotected = replace(
            self.config,
            precision="float32",
            subnormal_protection=False,
        )
        self.assertEqual(float(quantize(np.array([tiny]), protected)[0]), 0.0)
        self.assertNotEqual(
            float(quantize(np.array([tiny]), unprotected)[0]), 0.0
        )


if __name__ == "__main__":
    unittest.main()
