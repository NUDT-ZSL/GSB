from __future__ import annotations

import unittest

import numpy as np

from regularization_lab.config import RegularizerSpec
from regularization_lab.objective import evaluate_regularization, l1_subgradient


class ObjectiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.weights = np.asarray([2.0, -1.0, 0.0], dtype=np.float64)

    def test_l2_loss_and_gradient(self) -> None:
        result = evaluate_regularization(
            self.weights, RegularizerSpec("ridge", "l2", 0.5, 0.0)
        )
        self.assertAlmostEqual(result.loss, 1.25)
        self.assertEqual(result.l1_loss, 0.0)
        np.testing.assert_allclose(result.gradient, [1.0, -0.5, 0.0], rtol=0.0, atol=0.0)

    def test_l1_loss_gradient_and_zero_subgradient(self) -> None:
        result = evaluate_regularization(
            self.weights, RegularizerSpec("lasso", "l1", 0.3, 1.0)
        )
        self.assertAlmostEqual(result.loss, 0.9)
        self.assertEqual(result.l2_loss, 0.0)
        np.testing.assert_allclose(result.gradient, [0.3, -0.3, 0.0], rtol=0.0, atol=0.0)
        np.testing.assert_array_equal(l1_subgradient(np.asarray([0.0])), [0.0])

    def test_elastic_net_combines_both_terms_linearly(self) -> None:
        result = evaluate_regularization(
            self.weights, RegularizerSpec("elastic", "elastic_net", 0.4, 0.25)
        )
        self.assertAlmostEqual(result.l1_loss, 0.3)
        self.assertAlmostEqual(result.l2_loss, 0.75)
        self.assertAlmostEqual(result.loss, 1.05)
        np.testing.assert_allclose(
            result.gradient,
            [0.7, -0.4, 0.0],
            rtol=1e-15,
            atol=1e-15,
        )


if __name__ == "__main__":
    unittest.main()
