from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from flash_attn_cache import FlashAttentionEngine, QKVArrays

from tests.helpers import random_case


class ValidationTests(unittest.TestCase):
    def test_duplicate_case_id(self) -> None:
        case, arrays = random_case()
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            batch = engine.compute_batch([(case, arrays), (case, arrays)])
            self.assertFalse(batch.ok)
            issue = batch.errors[0]
            self.assertEqual(issue.code, "E_DUPLICATE_CASE_ID")
            self.assertEqual(issue.case_id, case.case_id)
            self.assertEqual(issue.field, "case_id")

    def test_shape_mismatch(self) -> None:
        case, arrays = random_case(seq_len=8)
        bad = QKVArrays(arrays.q[:, :7, :], arrays.k, arrays.v)
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            result = engine.compute(case, bad)
            self.assertFalse(result.ok)
            issue = next(
                item for item in result.errors if item.code == "E_SEQ_LEN_MISMATCH"
            )
            self.assertEqual(issue.case_id, case.case_id)
            self.assertEqual(issue.field, "q")

    def test_non_positive_head_dim_and_block_size(self) -> None:
        case, arrays = random_case(head_dim=0)
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            result = engine.compute(case, arrays)
            self.assertIn("E_INVALID_HEAD_DIM", result.error_codes)
        case, arrays = random_case(block_size=0)
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            result = engine.compute(case, arrays)
            self.assertIn("E_INVALID_BLOCK_SIZE", result.error_codes)

    def test_causal_mask_conflict(self) -> None:
        case, arrays = random_case(causal=True, dtype="float64")
        case = case.replace(mask_mode="none")
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            result = engine.compute(case, arrays)
            issue = next(
                item
                for item in result.errors
                if item.code == "E_CAUSAL_MASK_CONFLICT"
            )
            self.assertEqual(issue.field, "mask_mode")

    def test_unsupported_dtype_and_arrays(self) -> None:
        case, arrays = random_case(dtype="float64")
        unsupported_case = case.replace(dtype="int32")
        int_arrays = QKVArrays(
            arrays.q.astype(np.int32),
            arrays.k.astype(np.int32),
            arrays.v.astype(np.int32),
        )
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            result = engine.compute(unsupported_case, int_arrays)
            self.assertIn("E_UNSUPPORTED_DTYPE", result.error_codes)
            issue = next(
                item
                for item in result.errors
                if item.code == "E_UNSUPPORTED_DTYPE"
            )
            self.assertEqual(issue.case_id, unsupported_case.case_id)
            self.assertEqual(issue.field, "dtype")

    def test_non_finite_input_is_rejected(self) -> None:
        case, arrays = random_case()
        bad_q = arrays.q.copy()
        bad_q[0, 0, 0] = np.nan
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            result = engine.compute(
                case,
                QKVArrays(bad_q, arrays.k, arrays.v),
            )
            self.assertIn("E_NONFINITE_INPUT", result.error_codes)


if __name__ == "__main__":
    unittest.main()
