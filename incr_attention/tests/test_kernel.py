import unittest

import numpy as np

from _bootstrap import ROOT  # noqa: F401
from incrattn.kernel import attention_block
from incrattn.reference import reference_block


class KernelTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        self.q = rng.normal(size=(10, 4)).astype(np.float32)
        self.k = rng.normal(size=(10, 4)).astype(np.float32)
        self.v = rng.normal(size=(10, 4)).astype(np.float32)
        self.scale = 0.5

    def test_tail_block_is_exactly_trimmed(self):
        result = attention_block(2, self.q, self.k, self.v, 4, self.scale)
        self.assertEqual(result.output.shape, (2, 4))
        self.assertEqual(result.row_max.shape, (2,))
        self.assertEqual(result.kv_block_range, (0, 3))

    def test_float32_matches_float64_reference(self):
        for index in range(3):
            actual = attention_block(
                index, self.q, self.k, self.v, 4, self.scale
            )
            truth = reference_block(
                index, self.q, self.k, self.v, 4, self.scale
            )
            np.testing.assert_allclose(
                actual.output, truth.output, atol=1e-5, rtol=1e-5
            )
            np.testing.assert_allclose(
                actual.row_max, truth.row_max, atol=1e-6, rtol=1e-6
            )
            np.testing.assert_allclose(
                actual.exp_sum, truth.exp_sum, atol=1e-5, rtol=1e-5
            )

    def test_repeated_runs_are_bit_identical(self):
        first = attention_block(1, self.q, self.k, self.v, 4, self.scale)
        second = attention_block(1, self.q, self.k, self.v, 4, self.scale)
        np.testing.assert_array_equal(first.output, second.output)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_length_and_finite_validation(self):
        from incrattn.errors import (
            LengthMismatchError,
            NonFiniteInputError,
        )

        bad_v = self.v.copy()
        bad_k = self.k.copy()
        bad_k[0, 0] = np.nan
        with self.assertRaises(NonFiniteInputError):
            attention_block(0, self.q, bad_k, bad_v, 4, self.scale)

        skinny_k = self.k[:, :3]
        with self.assertRaises(LengthMismatchError):
            attention_block(0, self.q, skinny_k, self.v, 4, self.scale)


if __name__ == "__main__":
    unittest.main()
