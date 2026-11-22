from __future__ import annotations

import copy
import unittest

from sparse_inc.case import parse_case
from sparse_inc.codes import (
    E_CSR_STRUCT,
    E_DTYPE_UNSUPPORTED,
    E_NNZ_MISMATCH,
    E_SHAPE_INVALID,
)
from sparse_inc.errors import BusinessError

from tests.support import make_sample_directory, raw_case


class CaseValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp, cls.cases = make_sample_directory()
        cls.case = cls.cases["spmm_inc_demo"]
        cls.raw = raw_case(cls.case)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_valid_case_parses(self) -> None:
        parsed = parse_case(copy.deepcopy(self.raw))
        self.assertEqual(parsed.case_id, "spmm_inc_demo")
        self.assertEqual(parsed.operation, "spmm")
        self.assertEqual(len(parsed.write_batches), 5)

    def test_shape_invalid(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["shape_mnk"] = [20, 7, 0]
        self._assert_error(raw, E_SHAPE_INVALID, "shape_mnk")

    def test_dtype_unsupported(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["dtype"] = "float32"
        self._assert_error(raw, E_DTYPE_UNSUPPORTED, "csr_a.dtype")

    def test_indptr_non_monotonic(self) -> None:
        raw = copy.deepcopy(self.raw)
        indptr = raw["base_csr"]["csr_a"]["indptr"]
        indptr[2] = indptr[1] - 1
        self._assert_error(raw, E_CSR_STRUCT, "csr_a.indptr")

    def test_column_index_out_of_bounds(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["base_csr"]["csr_a"]["indices"][0] = raw["shape_mnk"][2]
        self._assert_error(raw, E_CSR_STRUCT, "csr_a.indices")

    def test_declared_nnz_mismatch(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["nnz_a"] += 1
        self._assert_error(raw, E_NNZ_MISMATCH, "csr_a.nnz")

    def test_csr_b_nnz_mismatch_is_reported(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["nnz_b"] += 1
        self._assert_error(raw, E_NNZ_MISMATCH, "csr_b.nnz")

    def test_indptr_wrong_length_is_structural(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["base_csr"]["csr_a"]["indptr"] = raw["base_csr"]["csr_a"]["indptr"][:-1]
        self._assert_error(raw, E_CSR_STRUCT, "csr_a.indptr")

    def test_fractional_indptr_is_rejected_not_truncated(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["base_csr"]["csr_a"]["indptr"][1] = float(
            raw["base_csr"]["csr_a"]["indptr"][1]
        ) + 0.5
        self._assert_error(raw, E_CSR_STRUCT, "csr_a.indptr[1]")

    def _assert_error(self, raw: dict, expected_code: str, segment: str) -> None:
        with self.assertRaises(BusinessError) as caught:
            parse_case(raw)
        error = caught.exception
        self.assertEqual(error.business_code, expected_code)
        self.assertEqual(error.case_id, "spmm_inc_demo")
        self.assertIn(segment, error.segment or "")
        self.assertTrue(error.message)


if __name__ == "__main__":
    unittest.main()
