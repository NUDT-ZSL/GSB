from __future__ import annotations

import copy
import unittest

import numpy as np

from sparse_spmm.attribution import _classification, attribute_row, precision_settings
from sparse_spmm.errors import ToleranceError
from sparse_spmm.kernels import chunked_row, reference_row
from sparse_spmm.layers import analyze_layers
from tests.helpers import load_config
from sparse_spmm.runner import prepare_case


class AttributionTests(unittest.TestCase):
    def test_tolerance_zero_is_rejected(self) -> None:
        config = load_config("sample_test.json")
        config["execution"]["tolerance"]["relative"] = 0.0
        with self.assertRaises(ToleranceError) as caught:
            precision_settings(config)
        self.assertEqual(caught.exception.business_code, "E_TOLERANCE_INVALID")

    def test_contribution_chain_is_auditable(self) -> None:
        case = prepare_case(load_config("sample_test.json"), load_config("strategies.json"))
        row = 0
        traced = chunked_row(
            case.csr,
            case.B,
            case.C,
            row=row,
            alpha=float(case.sample_config["alpha"]),
            beta=float(case.sample_config["beta"]),
            block_size=case.strategy["block_size"],
            order=case.strategy["order"],
            dtype=np.float32,
            trace=True,
        )
        reference = reference_row(
            case.csr,
            case.B,
            case.C,
            row=row,
            alpha=float(case.sample_config["alpha"]),
            beta=float(case.sample_config["beta"]),
            dtype=np.float64,
        )
        elements = attribute_row(
            case.csr,
            case.B,
            result=traced,
            reference64=reference.values,
            alpha=float(case.sample_config["alpha"]),
            settings=case.precision,
        )
        analyze_layers(elements, case.sample_config)
        valid_ids = set(case.csr.nonzero_ids[int(case.csr.indptr[row]) : int(case.csr.indptr[row + 1])])
        for element in elements:
            self.assertIn("layer_id", element)
            self.assertLessEqual(len(element["top_contributions"]), case.precision.attribution_top_k)
            previous = 0.0
            for contribution in element["top_contributions"]:
                self.assertIn(contribution["nz_id"], valid_ids)
                self.assertGreaterEqual(contribution["chunk_index"], 0)
                self.assertGreaterEqual(contribution["position_in_chunk"], 1)
                self.assertGreaterEqual(contribution["cumulative_share"], previous)
                self.assertLessEqual(contribution["cumulative_share"], 1.0 + 1e-15)
                previous = contribution["cumulative_share"]
            self.assertAlmostEqual(
                element["estimated_error_from_ledger"]
                + element["unattributed_nonlinear_residual"],
                element["computed_float32"] - element["reference_float64"],
                places=12,
            )

    def test_source_classifier_distinguishes_three_regimes(self) -> None:
        settings = precision_settings(load_config("sample_test.json"))
        single, _ = _classification(
            nnz=200,
            top_share=0.8,
            largest_term_share=0.3,
            cancellation_ratio=2.0,
            sign_changes=4,
            large_small_pairs=2,
            settings=settings,
        )
        cancellation, _ = _classification(
            nnz=200,
            top_share=0.2,
            largest_term_share=0.15,
            cancellation_ratio=25.0,
            sign_changes=5,
            large_small_pairs=4,
            settings=settings,
        )
        long_chain, _ = _classification(
            nnz=300,
            top_share=0.1,
            largest_term_share=0.05,
            cancellation_ratio=2.0,
            sign_changes=10,
            large_small_pairs=0,
            settings=settings,
        )
        self.assertEqual(single, "SINGLE_LARGE_NONZERO")
        self.assertEqual(cancellation, "ADJACENT_CANCELLATION")
        self.assertEqual(long_chain, "LONG_CHAIN_ACCUMULATION")


if __name__ == "__main__":
    unittest.main()
