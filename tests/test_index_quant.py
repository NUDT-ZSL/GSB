from __future__ import annotations

import unittest

import numpy as np

from embedding_ops.cli import sample_specs
from embedding_ops.config import NumericsConfig
from embedding_ops.errors import BusinessError
from embedding_ops.generator import generate_job_arrays
from embedding_ops.indexing import generate_index_array, validate_indices
from embedding_ops.pipeline import gather_embedding
from embedding_ops.quantize import (
    build_quantized_table,
    dequantize_table,
)


class IndexValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = sample_specs()[0]

    def test_padding_mask_and_digest_are_reproducible(self) -> None:
        first = generate_index_array(
            self.spec.index_set,
            vocab_size=self.spec.vocab_size,
            pad_index=self.spec.effective_pad_index,
            seed=self.spec.seed,
            job_id=self.spec.job_id,
        )
        second = generate_index_array(
            self.spec.index_set,
            vocab_size=self.spec.vocab_size,
            pad_index=self.spec.effective_pad_index,
            seed=self.spec.seed,
            job_id=self.spec.job_id,
        )
        np.testing.assert_array_equal(first, second)
        checked_a = validate_indices(
            first,
            vocab_size=self.spec.vocab_size,
            pad_index=self.spec.effective_pad_index,
            job_id=self.spec.job_id,
        )
        checked_b = validate_indices(
            second,
            vocab_size=self.spec.vocab_size,
            pad_index=self.spec.effective_pad_index,
            job_id=self.spec.job_id,
        )
        self.assertEqual(checked_a.pad_positions, [[1, 1], [4, 0]])
        self.assertEqual(checked_a.mask_digest, checked_b.mask_digest)
        self.assertEqual(checked_a.index_digest, checked_b.index_digest)

    def test_out_of_range_reports_first_position_and_job(self) -> None:
        indices = np.array([[1, 2], [16, -1]], dtype=np.int64)
        with self.assertRaises(BusinessError) as caught:
            validate_indices(
                indices,
                vocab_size=16,
                pad_index=0,
                job_id="oob-job",
            )
        error = caught.exception
        self.assertEqual(error.code, "E_INDEX_OUT_OF_RANGE")
        self.assertEqual(error.stage, "index_validation")
        self.assertEqual(error.job_id, "oob-job")
        self.assertEqual(error.detail["first_position"], 2)
        self.assertEqual(error.detail["position"], [1, 0])
        self.assertEqual(error.detail["value"], 16)

    def test_empty_index_reports_business_code(self) -> None:
        with self.assertRaises(BusinessError) as caught:
            validate_indices(
                np.empty((0, 1), dtype=np.int64),
                vocab_size=16,
                pad_index=0,
                job_id="empty-job",
            )
        self.assertEqual(caught.exception.code, "E_INDEX_EMPTY")
        self.assertEqual(caught.exception.stage, "index_validation")

    def test_invalid_pad_index_is_checked_before_oob(self) -> None:
        with self.assertRaises(BusinessError) as caught:
            validate_indices(
                np.array([[16]], dtype=np.int64),
                vocab_size=16,
                pad_index=16,
                job_id="pad-job",
            )
        self.assertEqual(caught.exception.code, "E_PAD_INDEX_INVALID")


class QuantizationAndGatherTests(unittest.TestCase):
    def test_qint_table_recomputes_independently(self) -> None:
        spec = sample_specs()[3]
        config = NumericsConfig()
        arrays = generate_job_arrays(spec)
        quantized = build_quantized_table(
            arrays.truth_table,
            spec=spec,
            config=config,
        )
        recomputed = dequantize_table(
            quantized.storage,
            table_dtype=quantized.table_dtype,
            quant_scale=quantized.quant_scale,
            quant_zero_point=quantized.quant_zero_point,
            config=config,
            job_id=spec.job_id,
        )
        np.testing.assert_array_equal(quantized.dequantized, recomputed)
        self.assertEqual(quantized.independent_recompute_diff, 0.0)
        self.assertEqual(quantized.clipped_values, 0)

    def test_quant_invalid_scale_and_zero_point(self) -> None:
        spec = sample_specs()[3]
        arrays = generate_job_arrays(spec)
        from dataclasses import replace

        for invalid in (
            replace(spec, quant_scale=0.0),
            replace(spec, quant_scale=-0.1),
            replace(spec, quant_zero_point=128),
        ):
            with self.subTest(quant_scale=invalid.quant_scale):
                with self.assertRaises(BusinessError) as caught:
                    build_quantized_table(
                        arrays.truth_table,
                        spec=invalid,
                        config=NumericsConfig(),
                    )
                self.assertEqual(caught.exception.code, "E_QUANT_INVALID")
                self.assertEqual(caught.exception.stage, "table_build")

    def test_rounding_mode_changes_quantized_storage(self) -> None:
        spec = sample_specs()[3]
        arrays = generate_job_arrays(spec)
        nearest = build_quantized_table(
            arrays.truth_table,
            spec=spec,
            config=NumericsConfig(rounding_mode="nearest_even"),
        )
        truncate = build_quantized_table(
            arrays.truth_table,
            spec=spec,
            config=NumericsConfig(rounding_mode="truncate"),
        )
        self.assertNotEqual(nearest.storage_digest, truncate.storage_digest)

    def test_gather_axis_zero_and_transposed_axis_one_match(self) -> None:
        table = np.arange(20, dtype=np.float64).reshape(5, 4)
        indices = np.array([[1, 3], [0, 4]], dtype=np.int64)
        axis_zero = gather_embedding(table, indices, 0, job_id="axis0")
        axis_one = gather_embedding(
            np.ascontiguousarray(table.T),
            indices,
            1,
            job_id="axis1",
        )
        np.testing.assert_array_equal(axis_zero, axis_one)
        self.assertEqual(axis_zero.shape, (2, 2, 4))

    def test_gather_bound_error_is_attributed(self) -> None:
        with self.assertRaises(BusinessError) as caught:
            gather_embedding(
                np.zeros((4, 3)),
                np.array([[4]], dtype=np.int64),
                0,
                job_id="gather-oob",
            )
        self.assertEqual(caught.exception.code, "E_INDEX_OUT_OF_RANGE")
        self.assertEqual(caught.exception.stage, "gather")
        self.assertEqual(caught.exception.detail["position"], [0, 0])


if __name__ == "__main__":
    unittest.main()
