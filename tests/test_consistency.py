from __future__ import annotations

import unittest
from unittest import mock

from sparse_precision.api import execute_case
from sparse_precision.codes import E_INCONSISTENT

from tests.helpers import metadata_for


class ConsistencyTests(unittest.TestCase):
    def test_self_check_categories_pass(self) -> None:
        report = execute_case(metadata_for("spmm_dense_7"))
        consistency = report["consistency"]
        self.assertTrue(consistency["enabled"])
        self.assertTrue(consistency["passed"])
        categories = {item["category"] for item in consistency["checks"]}
        self.assertEqual(
            categories,
            {
                "spmm_vs_degenerate_spspmm",
                "transpose_semantics",
                "row_major_column_major_view",
                "partial_reduction_recompute",
            },
        )
        for item in consistency["checks"]:
            self.assertTrue(item["passed"], item)
        self.assertEqual(
            report["diagnostics"]["reduction_recompute_max_abs_diff"], 0.0
        )

    def test_inconsistent_check_returns_business_code(self) -> None:
        fake = {
            "enabled": True,
            "passed": False,
            "failed_categories": ["transpose_semantics"],
            "checks": [
                {
                    "category": "transpose_semantics",
                    "passed": False,
                    "max_abs_diff": 1.0,
                }
            ],
        }
        with mock.patch(
            "sparse_precision.api.run_consistency_checks",
            return_value=fake,
        ):
            report = execute_case(metadata_for("spmm_dense_7"))
        self.assertEqual(report["code"], E_INCONSISTENT)
        self.assertEqual(report["phase"], "consistency")
        self.assertEqual(
            report["details"]["failed_categories"], ["transpose_semantics"]
        )


if __name__ == "__main__":
    unittest.main()

