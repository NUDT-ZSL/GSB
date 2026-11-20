from __future__ import annotations

import unittest

import numpy as np

from embedding_ops.config import NumericsConfig
from embedding_ops.pipeline import reduce_and_weight


class ReductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = np.array(
            [
                [1.0, 2.0, 3.0],
                [4.0, 5.0, 6.0],
                [7.0, 8.0, 9.0],
                [10.0, 11.0, 12.0],
            ],
            dtype=np.float64,
        )
        self.weights = np.array(
            [[1.0, -1.0, 0.5], [0.25, 0.5, 1.0]],
            dtype=np.float64,
        )

    def _gather(self, indices: np.ndarray) -> np.ndarray:
        return self.table[indices]

    def test_sum_keeps_duplicate_hit_details(self) -> None:
        indices = np.array([[1, 1], [2, 0]], dtype=np.int64)
        pad_mask = np.zeros(indices.shape, dtype=bool)
        result = reduce_and_weight(
            self._gather(indices),
            indices,
            pad_mask,
            self.weights,
            reduction_mode="sum",
            pad_policy="exclude",
            config=NumericsConfig(),
            job_id="sum",
        )
        expected = np.stack(
            [
                self.table[1] * self.weights[0] * 2.0,
                self.table[2] * self.weights[1] + self.table[0] * self.weights[1],
            ]
        )
        np.testing.assert_allclose(result.output, expected, rtol=0.0, atol=0.0)
        index_one = next(
            detail for detail in result.weight_details if detail["index"] == 1
        )
        self.assertEqual(index_one["hits"], 2)
        self.assertEqual(index_one["positions"], [[0, 0], [0, 1]])
        self.assertEqual(index_one["reduction_weights"], [1.0, 1.0])

    def test_padding_policies_change_mean_denominator(self) -> None:
        indices = np.array([[1, 0], [2, 0]], dtype=np.int64)
        pad_mask = np.array([[False, True], [False, False]], dtype=bool)
        gathered = self._gather(indices)
        excluded = reduce_and_weight(
            gathered,
            indices,
            pad_mask,
            self.weights,
            reduction_mode="mean",
            pad_policy="exclude",
            config=NumericsConfig(pad_policy="exclude"),
            job_id="mean-exclude",
        )
        zero_weighted = reduce_and_weight(
            gathered,
            indices,
            pad_mask,
            self.weights,
            reduction_mode="mean",
            pad_policy="zero_weight",
            config=NumericsConfig(pad_policy="zero_weight"),
            job_id="mean-zero",
        )
        np.testing.assert_allclose(
            excluded.output[0],
            self.table[1] * self.weights[0],
            rtol=0.0,
            atol=0.0,
        )
        np.testing.assert_allclose(
            zero_weighted.output[0],
            self.table[1] * self.weights[0] * 0.5,
            rtol=0.0,
            atol=0.0,
        )
        np.testing.assert_allclose(
            zero_weighted.output[1],
            excluded.output[1],
            rtol=0.0,
            atol=0.0,
        )

    def test_first_and_concat_modes(self) -> None:
        indices = np.array([[1, 0], [2, 3]], dtype=np.int64)
        pad_mask = np.array([[False, True], [False, False]], dtype=bool)
        gathered = self._gather(indices)
        first = reduce_and_weight(
            gathered,
            indices,
            pad_mask,
            self.weights,
            reduction_mode="first",
            pad_policy="exclude",
            config=NumericsConfig(),
            job_id="first",
        )
        np.testing.assert_allclose(
            first.output[0],
            self.table[1] * self.weights[0],
            rtol=0.0,
            atol=0.0,
        )
        concat = reduce_and_weight(
            gathered,
            indices,
            pad_mask,
            self.weights,
            reduction_mode="concat",
            pad_policy="exclude",
            config=NumericsConfig(),
            job_id="concat",
        )
        self.assertEqual(concat.output.shape, (2, 6))
        np.testing.assert_allclose(
            concat.output[1, 0:3],
            self.table[2] * self.weights[1],
            rtol=0.0,
            atol=0.0,
        )
        np.testing.assert_allclose(
            concat.output[1, 3:6],
            self.table[3] * self.weights[1],
            rtol=0.0,
            atol=0.0,
        )

    def test_first_skips_padding_even_with_zero_weight_policy(self) -> None:
        indices = np.array([[0, 1]], dtype=np.int64)
        pad_mask = np.array([[True, False]], dtype=bool)
        result = reduce_and_weight(
            self._gather(indices),
            indices,
            pad_mask,
            self.weights[:1, :],
            reduction_mode="first",
            pad_policy="zero_weight",
            config=NumericsConfig(pad_policy="zero_weight"),
            job_id="first-zero",
        )
        np.testing.assert_allclose(
            result.output[0],
            self.table[1] * self.weights[0],
            rtol=0.0,
            atol=0.0,
        )

    def test_accumulation_rounding_is_switchable(self) -> None:
        indices = np.array([[1, 0]], dtype=np.int64)
        gathered = self._gather(indices)
        weights = np.array([[0.3, -0.7, 0.9]], dtype=np.float64)
        exact = reduce_and_weight(
            gathered,
            indices,
            np.zeros(indices.shape, dtype=bool),
            weights,
            reduction_mode="sum",
            pad_policy="exclude",
            config=NumericsConfig(),
            job_id="exact",
        )
        rounded = reduce_and_weight(
            gathered,
            indices,
            np.zeros(indices.shape, dtype=bool),
            weights,
            reduction_mode="sum",
            pad_policy="exclude",
            config=NumericsConfig(
                accum_rounding_enabled=True,
                accum_rounding_step=1.0,
            ),
            job_id="rounded",
        )
        self.assertFalse(np.array_equal(exact.output, rounded.output))
        self.assertTrue(rounded.accumulation.rounding_applied)


if __name__ == "__main__":
    unittest.main()
