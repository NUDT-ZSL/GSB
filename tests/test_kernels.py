from __future__ import annotations

import unittest

import numpy as np

from sparse_spmm.csr import CSRMatrix
from sparse_spmm.kernels import (
    VALID_ORDERS,
    chunked_row,
    reference_row,
    validate_block_size,
)
from sparse_spmm.errors import BlockParameterError
from tests.helpers import load_config
from sparse_spmm.runner import prepare_case


def manual_csr(
    *,
    shape: tuple[int, int],
    indptr: list[int],
    indices: list[int],
    data: list[float],
) -> CSRMatrix:
    ids = []
    ordinals = np.arange(len(indices), dtype=np.int64)
    for row in range(shape[0]):
        for p in range(indptr[row], indptr[row + 1]):
            col = indices[p]
            ids.append(f"nz-r{row:06d}-c{col:06d}-g{p:09d}")
    return CSRMatrix(
        shape=shape,
        indptr=np.asarray(indptr, dtype=np.int64),
        indices=np.asarray(indices, dtype=np.int32),
        data=np.asarray(data, dtype=np.float32),
        nonzero_ids=ids,
        generation_ordinals=ordinals,
    )


class KernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.csr = manual_csr(
            shape=(3, 6),
            indptr=[0, 4, 4, 7],
            indices=[0, 1, 3, 5, 1, 2, 5],
            data=[1.0, -2.0, 0.5, 1.25, 3.0, 4.0, -1.0],
        )
        self.B = np.asarray(
            [
                [1.0, -2.0],
                [3.0, 0.5],
                [-1.0, 4.0],
                [2.0, 0.25],
                [5.0, -3.0],
                [-0.5, 6.0],
            ],
            dtype=np.float32,
        )
        self.C = np.asarray([[0.25, -1.0], [2.0, 0.5], [-3.0, 0.0]], dtype=np.float32)

    def test_float64_reference_matches_dense_formula(self) -> None:
        dense = np.zeros(self.csr.shape, dtype=np.float64)
        for row in range(self.csr.rows):
            for p in range(int(self.csr.indptr[row]), int(self.csr.indptr[row + 1])):
                dense[row, int(self.csr.indices[p])] = self.csr.data[p]
        expected = 1.3 * dense @ self.B.astype(np.float64) + (-0.2) * self.C.astype(np.float64)
        for row in range(self.csr.rows):
            result = reference_row(
                self.csr,
                self.B,
                self.C,
                row=row,
                alpha=1.3,
                beta=-0.2,
                dtype=np.float64,
            )
            np.testing.assert_allclose(result.values, expected[row], rtol=1e-14, atol=1e-14)
            self.assertEqual(result.record["order_id"], "REF_COL_ASC")
            self.assertEqual(len(result.record["order_digest"]), 64)

    def test_float32_reference_is_close_and_records_metadata(self) -> None:
        dense = np.zeros(self.csr.shape, dtype=np.float64)
        for row in range(self.csr.rows):
            for p in range(int(self.csr.indptr[row]), int(self.csr.indptr[row + 1])):
                dense[row, int(self.csr.indices[p])] = self.csr.data[p]
        expected = 1.3 * dense @ self.B.astype(np.float64) + (-0.2) * self.C.astype(np.float64)
        for row in range(self.csr.rows):
            result = reference_row(
                self.csr,
                self.B,
                self.C,
                row=row,
                alpha=1.3,
                beta=-0.2,
                dtype=np.float32,
            )
            np.testing.assert_allclose(result.values, expected[row], rtol=1e-5, atol=1e-5)
            self.assertEqual(result.record["nnz"], int(self.csr.indptr[row + 1] - self.csr.indptr[row]))
            self.assertEqual(len(result.record["result_fingerprint"]), 64)

    def test_chunk_boundaries_and_zero_row(self) -> None:
        row = 2
        result = chunked_row(
            self.csr,
            self.B,
            self.C,
            row=row,
            alpha=1.0,
            beta=0.0,
            block_size=2,
            order="asc",
            dtype=np.float32,
        )
        self.assertEqual(result.record["chunk_ranges"], [[4, 6], [6, 7]])
        zero = chunked_row(
            self.csr,
            self.B,
            self.C,
            row=1,
            alpha=1.0,
            beta=0.0,
            block_size=2,
            order="b_abs_desc",
            dtype=np.float32,
        )
        self.assertEqual(zero.record["nnz"], 0)
        np.testing.assert_array_equal(zero.values, np.zeros(2, dtype=np.float32))

    def test_invalid_block_size(self) -> None:
        with self.assertRaises(BlockParameterError) as caught:
            validate_block_size(0, K=6)
        self.assertEqual(caught.exception.business_code, "E_BLOCK_INVALID")

    def test_all_orders_show_observable_element_differences(self) -> None:
        case = prepare_case(load_config("sample_test.json"), load_config("strategies.json"))
        reference32 = np.stack(
            [
                reference_row(
                    case.csr,
                    case.B,
                    case.C,
                    row=row,
                    alpha=float(case.sample_config["alpha"]),
                    beta=float(case.sample_config["beta"]),
                    dtype=np.float32,
                ).values
                for row in range(case.csr.rows)
            ]
        )
        for order in VALID_ORDERS:
            matrix = np.stack(
                [
                    chunked_row(
                        case.csr,
                        case.B,
                        case.C,
                        row=row,
                        alpha=float(case.sample_config["alpha"]),
                        beta=float(case.sample_config["beta"]),
                        block_size=case.strategy["block_size"],
                        order=order,
                        dtype=np.float32,
                    ).values
                    for row in range(case.csr.rows)
                ]
            )
            self.assertGreater(int(np.count_nonzero(matrix != reference32)), 0, msg=order)

    def test_float32_and_float64_strategy_tiers_differ(self) -> None:
        case = prepare_case(load_config("sample_test.json"), load_config("strategies.json"))
        selected32 = np.stack(
            [
                chunked_row(
                    case.csr,
                    case.B,
                    case.C,
                    row=row,
                    alpha=float(case.sample_config["alpha"]),
                    beta=float(case.sample_config["beta"]),
                    block_size=case.strategy["block_size"],
                    order=case.strategy["order"],
                    dtype=np.float32,
                ).values
                for row in range(case.csr.rows)
            ]
        )
        selected64 = np.stack(
            [
                chunked_row(
                    case.csr,
                    case.B,
                    case.C,
                    row=row,
                    alpha=float(case.sample_config["alpha"]),
                    beta=float(case.sample_config["beta"]),
                    block_size=case.strategy["block_size"],
                    order=case.strategy["order"],
                    dtype=np.float64,
                ).values
                for row in range(case.csr.rows)
            ]
        )
        self.assertGreater(int(np.count_nonzero(selected32.astype(np.float64) != selected64)), 0)


if __name__ == "__main__":
    unittest.main()

