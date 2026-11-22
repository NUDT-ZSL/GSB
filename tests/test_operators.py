from __future__ import annotations

import unittest

import numpy as np
from scipy import sparse

from sparse_inc import execute_case, multiply, spmm, spspmm
from sparse_inc.codes import OK

from tests.support import make_sample_directory


class OperatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp, cls.cases = make_sample_directory()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_spmm_matches_float64_dense_reference(self) -> None:
        case = self.cases["spmm_inc_demo"]
        actual = spmm(case.a, case.b)
        expected = case.a.to_dense() @ case.b.to_dense()
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-12)

    def test_spspmm_returns_csr_and_matches_dense_reference(self) -> None:
        case = self.cases["spspmm_inc_demo"]
        actual = spspmm(case.a, case.b)
        self.assertTrue(sparse.isspmatrix_csr(actual))
        self.assertEqual(actual.dtype, np.float64)
        expected = case.a.to_dense() @ case.b.to_dense()
        np.testing.assert_allclose(actual.toarray(), expected, rtol=0, atol=1e-12)

    def test_execute_case_unified_entry(self) -> None:
        case = self.cases["spspmm_inc_demo"]
        service = execute_case(case)
        self.assertEqual(service.ledger.version, 4)
        self.assertEqual(service.self_check()["business_code"], OK)
        self.assertTrue(service.self_check()["deviation"]["within_tolerance"])

    def test_multiply_dispatches_both_modes(self) -> None:
        spmm_case = self.cases["spmm_inc_demo"]
        assert multiply(spmm_case.a, spmm_case.b, operation="spmm").shape == (20, 7)
        spspmm_case = self.cases["spspmm_inc_demo"]
        sparse_result = multiply(spspmm_case.a, spspmm_case.b, operation="spspmm")
        self.assertTrue(sparse.isspmatrix_csr(sparse_result))


if __name__ == "__main__":
    unittest.main()
