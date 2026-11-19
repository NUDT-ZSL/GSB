from __future__ import annotations

import unittest

from sparse_precision.api import execute_case
from sparse_precision.codes import OK
from sparse_precision.generator import build_operands
from sparse_precision.models import OperatorConfig
from sparse_precision.validation import parse_case_spec

from tests.helpers import metadata_for


class KernelTests(unittest.TestCase):
    def test_spmm_and_spspmm_meet_tolerance(self) -> None:
        for case_id in (
            "spmm_dense_7",
            "spspmm_balanced_40",
            "spspmm_float32_mini",
        ):
            with self.subTest(case_id=case_id):
                report = execute_case(metadata_for(case_id))
                self.assertEqual(report["code"], OK)
                self.assertTrue(report["diagnostics"]["passed"])
                self.assertEqual(report["diagnostics"]["violation_count"], 0)
                self.assertEqual(
                    report["execution"]["nnz_a_processed"],
                    report["execution"]["nnz_a_declared"],
                )
                self.assertTrue(report["execution"]["coverage_ok"])
                self.assertTrue(report["consistency"]["passed"])

    def test_batch_semantics(self) -> None:
        report = execute_case(metadata_for("spspmm_batched_24"))
        self.assertEqual(report["code"], OK)
        self.assertEqual(report["batch_size"], 2)
        self.assertEqual(report["result_summary"]["shape"], [2, 48, 24])
        self.assertEqual(report["execution"]["nnz_a_processed"], 600)

    def test_aggregation_path_executes(self) -> None:
        report = execute_case(metadata_for("spspmm_aggregated_32"))
        self.assertEqual(report["code"], OK)
        self.assertTrue(report["strategy"]["use_aggregation_path"])
        self.assertGreater(report["execution"]["aggregation_entries"], 0)
        self.assertEqual(
            report["execution"]["stage_methods"]["sparse_index_summation"],
            "aggregated COO grouping",
        )

    def test_result_and_strategy_checksums_are_stable(self) -> None:
        metadata = metadata_for("spmm_dense_7")
        first = execute_case(metadata)
        second = execute_case(metadata)
        self.assertEqual(
            first["strategy"]["strategy_sha256"],
            second["strategy"]["strategy_sha256"],
        )
        self.assertEqual(
            first["result_summary"]["checksum"],
            second["result_summary"]["checksum"],
        )
        self.assertEqual(
            first["execution"]["partial_checksum"],
            second["execution"]["partial_checksum"],
        )

    def test_uncompensated_variant_still_uses_reference_tolerance(self) -> None:
        config = OperatorConfig(
            compensated_sum=False,
            enable_self_check=False,
        )
        report = execute_case(metadata_for("spmm_vector_1"), config=config)
        self.assertEqual(report["code"], OK)
        self.assertFalse(report["strategy"]["compensated_sum"])
        self.assertEqual(
            report["diagnostics"]["tolerance"]["atol"], config.atol
        )

    def test_direct_supplied_scipy_operands(self) -> None:
        spmm_metadata = metadata_for("spmm_dense_7")
        spmm_spec = parse_case_spec(spmm_metadata)
        spmm_operands = build_operands(spmm_spec)
        spmm_report = execute_case(
            spmm_metadata,
            spmm_operands.a[0],
            spmm_operands.b_dense[0],
        )
        self.assertEqual(spmm_report["code"], OK)

        sparse_metadata = metadata_for("spspmm_balanced_40")
        sparse_spec = parse_case_spec(sparse_metadata)
        sparse_operands = build_operands(sparse_spec)
        sparse_report = execute_case(
            sparse_metadata,
            sparse_operands.a[0],
            sparse_operands.b_sparse[0],
        )
        self.assertEqual(sparse_report["code"], OK)


if __name__ == "__main__":
    unittest.main()

