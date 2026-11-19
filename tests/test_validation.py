from __future__ import annotations

import copy
import unittest

import numpy as np

from sparse_precision.api import execute_case
from sparse_precision.codes import (
    E_CONFIG_INVALID,
    E_CSR_INVALID,
    E_DTYPE_UNSUPPORTED,
    E_SHAPE_INVALID,
)
from sparse_precision.generator import build_operands
from sparse_precision.generator import densify_reference
from sparse_precision.models import OperatorConfig
from sparse_precision.validation import parse_case_spec

from tests.helpers import metadata_for


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.metadata = metadata_for("spmm_dense_7")
        cls.spec = parse_case_spec(cls.metadata)
        cls.operands = build_operands(cls.spec)
        cls.config = OperatorConfig(enable_self_check=False, diagnose=False)

    def _execute(self, metadata, a, b):
        return execute_case(
            metadata,
            a,
            b,
            config=self.config,
        )

    def test_invalid_shape_has_case_and_field(self) -> None:
        metadata = copy.deepcopy(self.metadata)
        metadata["shape_mnk"] = [0, 96, 7]
        metadata["checksums"] = {}
        report = self._execute(
            metadata, self.operands.a, self.operands.b_dense
        )
        self.assertEqual(report["code"], E_SHAPE_INVALID)
        self.assertEqual(report["case_id"], "spmm_dense_7")
        self.assertEqual(report["field"], "shape_mnk")

    def test_non_integer_shape_dimension_is_shape_error(self) -> None:
        metadata = copy.deepcopy(self.metadata)
        metadata["shape_mnk"] = ["72", 96, 7]
        metadata["checksums"] = {}
        report = self._execute(
            metadata, self.operands.a, self.operands.b_dense
        )
        self.assertEqual(report["code"], E_SHAPE_INVALID)
        self.assertEqual(report["field"], "shape_mnk")

    def test_unsupported_dtype(self) -> None:
        metadata = copy.deepcopy(self.metadata)
        metadata["dtype"] = "complex128"
        report = self._execute(
            metadata, self.operands.a, self.operands.b_dense
        )
        self.assertEqual(report["code"], E_DTYPE_UNSUPPORTED)
        self.assertEqual(report["field"], "dtype")

    def test_unsupported_operand_dtype(self) -> None:
        a_longdouble = tuple(
            item.astype(np.longdouble) for item in self.operands.a
        )
        report = self._execute(
            self.metadata, a_longdouble, self.operands.b_dense
        )
        self.assertEqual(report["code"], E_DTYPE_UNSUPPORTED)
        self.assertEqual(report["field"], "a[0].dtype")

    def test_nonmonotonic_indptr(self) -> None:
        a_bad = self.operands.a[0].copy()
        a_bad.indptr[1] = -1
        report = self._execute(
            self.metadata, [a_bad], self.operands.b_dense
        )
        self.assertEqual(report["code"], E_CSR_INVALID)
        self.assertEqual(report["case_id"], "spmm_dense_7")
        self.assertIn("indptr", report["field"])

    def test_out_of_bounds_index(self) -> None:
        a_bad = self.operands.a[0].copy()
        a_bad.indices[0] = self.spec.k + 3
        report = self._execute(
            self.metadata, [a_bad], self.operands.b_dense
        )
        self.assertEqual(report["code"], E_CSR_INVALID)
        self.assertIn("indices", report["field"])

    def test_declared_nnz_mismatch(self) -> None:
        metadata = copy.deepcopy(self.metadata)
        metadata["nnz_a"] += 1
        metadata["checksums"] = {}
        report = self._execute(
            metadata, self.operands.a, self.operands.b_dense
        )
        self.assertEqual(report["code"], E_CSR_INVALID)
        self.assertEqual(report["field"], "nnz_a")
        self.assertEqual(report["details"]["declared"], self.spec.nnz_a + 1)

    def test_declared_nnz_b_mismatch(self) -> None:
        metadata = metadata_for("spspmm_balanced_40")
        spec = parse_case_spec(metadata)
        operands = build_operands(spec)
        metadata["nnz_b"] += 1
        metadata["checksums"] = {}
        report = execute_case(
            metadata,
            operands.a,
            operands.b_sparse,
            config=self.config,
        )
        self.assertEqual(report["code"], E_CSR_INVALID)
        self.assertEqual(report["field"], "nnz_b")
        self.assertEqual(report["details"]["declared"], spec.nnz_b + 1)

    def test_operand_shape_mismatch(self) -> None:
        metadata = copy.deepcopy(self.metadata)
        metadata["shape_mnk"] = [73, 96, 7]
        metadata["checksums"] = {}
        report = self._execute(
            metadata, self.operands.a, self.operands.b_dense
        )
        self.assertEqual(report["code"], E_SHAPE_INVALID)
        self.assertIn("shape", report["field"])

    def test_invalid_config_is_typed(self) -> None:
        report = execute_case(
            self.metadata,
            config={"atol": "not-a-number"},
        )
        self.assertEqual(report["code"], E_CONFIG_INVALID)
        self.assertEqual(report["case_id"], "spmm_dense_7")
        self.assertEqual(report["field"], "atol")

    def test_non_reconstructable_reference_is_rejected(self) -> None:
        reference = densify_reference(self.spec, self.operands)
        reference[0, 0, 0] += 1.0
        report = execute_case(
            self.metadata,
            reference=reference,
            config=self.config,
        )
        self.assertEqual(report["code"], E_CSR_INVALID)
        self.assertEqual(report["field"], "reference")
        self.assertIn("bitwise identical", report["message"])


if __name__ == "__main__":
    unittest.main()

