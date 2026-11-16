from __future__ import annotations

import copy
import unittest

import numpy as np

from sparse_spmm.csr import CSRMatrix, generate_csr, structure_fingerprint, validate_csr
from sparse_spmm.errors import CSRValidationError
from tests.helpers import load_config


def clone_csr(csr: CSRMatrix) -> CSRMatrix:
    return CSRMatrix(
        shape=csr.shape,
        indptr=csr.indptr.copy(),
        indices=csr.indices.copy(),
        data=csr.data.copy(),
        nonzero_ids=list(csr.nonzero_ids),
        generation_ordinals=csr.generation_ordinals.copy(),
    )


class CSRConstructionTests(unittest.TestCase):
    def test_all_distributions_are_sorted_and_reproducible(self) -> None:
        for name in ("sample_uniform.json", "sample_powerlaw.json", "sample_scsr.json"):
            with self.subTest(config=name):
                config = load_config(name)
                first, first_meta = generate_csr(config)
                second, second_meta = generate_csr(config)
                validate_csr(first)
                self.assertEqual(first_meta["structure_fingerprint"], second_meta["structure_fingerprint"])
                self.assertEqual(first_meta["data_fingerprint"], second_meta["data_fingerprint"])
                self.assertEqual(first.nonzero_ids, second.nonzero_ids)
                self.assertEqual(len(set(first.nonzero_ids)), first.nnz)
                self.assertEqual(len(first.nonzero_ids), first.nnz)
                for row in range(first.rows):
                    start = int(first.indptr[row])
                    end = int(first.indptr[row + 1])
                    cols = first.indices[start:end]
                    self.assertTrue(np.all(np.diff(cols) > 0))
                    self.assertEqual(len(cols), int(first_meta["row_nnz"][row]))
                for nz_id, position in zip(first.nonzero_ids, range(first.nnz)):
                    self.assertRegex(nz_id, r"^nz-r\d{6}-c\d{6}-g\d{9}$")

    def test_extreme_structure_spans_zero_to_thousands(self) -> None:
        csr, metadata = generate_csr(load_config("sample_scsr.json"))
        row_nnz = csr.row_nnz()
        self.assertEqual(int(row_nnz.min()), 0)
        self.assertGreaterEqual(int(row_nnz.max()), 3000)
        self.assertGreaterEqual(metadata["zero_row_count"], 1)
        self.assertEqual(metadata["nnz_sum"], csr.nnz)

    def test_invalid_indptr_is_e_csr_invalid(self) -> None:
        csr, _ = generate_csr(load_config("sample_uniform.json"))
        bad = clone_csr(csr)
        bad.indptr[1] = bad.indptr[1] + 1
        with self.assertRaises(CSRValidationError) as caught:
            validate_csr(bad)
        self.assertEqual(caught.exception.business_code, "E_CSR_INVALID")

    def test_out_of_range_column_is_e_csr_invalid(self) -> None:
        csr, _ = generate_csr(load_config("sample_uniform.json"))
        bad = clone_csr(csr)
        bad.indices[0] = -1
        with self.assertRaises(CSRValidationError) as caught:
            validate_csr(bad)
        self.assertEqual(caught.exception.business_code, "E_CSR_INVALID")

    def test_nnz_count_mismatch_is_e_csr_invalid(self) -> None:
        csr, _ = generate_csr(load_config("sample_uniform.json"))
        bad = clone_csr(csr)
        bad.data = bad.data[:-1]
        with self.assertRaises(CSRValidationError) as caught:
            validate_csr(bad)
        self.assertEqual(caught.exception.business_code, "E_CSR_INVALID")

    def test_non_integer_offsets_are_e_csr_invalid(self) -> None:
        csr, _ = generate_csr(load_config("sample_uniform.json"))
        bad = clone_csr(csr)
        bad.indptr = bad.indptr.astype(np.float64)
        with self.assertRaises(CSRValidationError) as caught:
            validate_csr(bad)
        self.assertEqual(caught.exception.business_code, "E_CSR_INVALID")

    def test_duplicate_nonzero_ids_are_e_csr_invalid(self) -> None:
        csr, _ = generate_csr(load_config("sample_uniform.json"))
        bad = clone_csr(csr)
        bad.nonzero_ids[1] = bad.nonzero_ids[0]
        with self.assertRaises(CSRValidationError) as caught:
            validate_csr(bad)
        self.assertEqual(caught.exception.business_code, "E_CSR_INVALID")

    def test_fingerprint_changes_with_seed(self) -> None:
        config = load_config("sample_uniform.json")
        first, _ = generate_csr(config)
        changed = copy.deepcopy(config)
        changed["seed"] += 1
        second, _ = generate_csr(changed)
        self.assertNotEqual(structure_fingerprint(first), structure_fingerprint(second))


if __name__ == "__main__":
    unittest.main()
